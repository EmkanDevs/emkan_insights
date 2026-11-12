from __future__ import annotations

import hashlib
import logging
from time import monotonic

import frappe
from frappe.utils import cint, nowdate

from emkan_insights.sap.mapping_order import mapping_sort_key
from emkan_insights.sap.services import get_mapped_data
from emkan_insights.sap.sync import sync_mapping


FETCH_LOCK_TTL = 2 * 60 * 60
CONFIGURATION_SYNC_TIMEOUT = 24 * 60 * 60


def enqueue_sap_synchronization() -> dict[str, int]:
	rows = frappe.get_all(
		"External Site Configuration CT",
		filters={
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
			"sap_mapping": ["is", "set"],
		},
		fields=["name", "parent", "sap_mapping"],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	)
	enabled_mappings = set(
		frappe.get_all("SAP Doctype Mapping", filters={"enabled": 1}, pluck="name")
	)
	configurations = sorted({row.parent for row in rows if row.sap_mapping in enabled_mappings})
	if not configurations:
		return {"queued": 0}
	job = frappe.enqueue(
		"emkan_insights.sap.tasks.synchronize_sap_configurations",
		queue="long",
		timeout=CONFIGURATION_SYNC_TIMEOUT,
		job_id="sap_synchronize_all",
		deduplicate=True,
		configuration_names=configurations,
	)
	return {"queued": int(bool(job))}


def enqueue_sap_master_sync() -> dict[str, int]:
	configurations = frappe.get_all(
		"External SAP Configuration",
		pluck="name",
		order_by="creation asc, name asc",
	)
	queued = 0
	for configuration_name in configurations:
		job = frappe.enqueue(
			"emkan_insights.sap.tasks.sync_sap_configuration",
			queue="long",
			timeout=FETCH_LOCK_TTL,
			job_id=_configuration_sync_job_id(configuration_name),
			deduplicate=True,
			configuration_name=configuration_name,
		)
		queued += bool(job)
	return {"queued": queued}


def synchronize_sap_data(
	row_name: str, *, continue_on_sync_error: bool = False, fetch_only: bool = False,
	from_date: str | None = None, to_date: str | None = None,
) -> dict:
	if bool(from_date) != bool(to_date):
		raise frappe.ValidationError("from_date and to_date must be provided together.")
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")

	# Share the lock with workers already running the previous job name.
	lock_key = _mapping_lock_key(row.name)
	if not frappe.cache().set(lock_key, "1", nx=True, ex=FETCH_LOCK_TTL):
		result = {"skipped": True, "reason": "A synchronization is already running for this mapping."}
		_log_fetch(row, "Skipped", result=result)
		return result
	started = monotonic()
	phase = "fetch"
	try:
		_log_fetch(row, "Started")
		mapping = frappe.get_doc("SAP Doctype Mapping", row.sap_mapping)
		fetch_options = {"from_date": from_date, "to_date": to_date} if from_date else {}
		if not fetch_options and not mapping.full_snapshot_sync and not getattr(mapping, "cursor_field", None) and (
			_has_staged_mapping_errors(mapping, row.parent)
			or (getattr(row, "last_sync", None) and _has_empty_staging(mapping, row.parent))
		):
			fetch_options = {"from_date": "2000-01-01", "to_date": nowdate()}
		result = get_mapped_data(
			row.sap_mapping,
			configuration_name=row.parent,
			row_name=row.name,
			**fetch_options,
		)
		if result.get("failed"):
			raise frappe.ValidationError(
			f"SAP fetch failed for {row.sap_mapping}: {result['failed']}"
		)
		status = "Skipped" if result.get("skipped") else "Completed"
		_log_fetch(row, status, result=result, elapsed=monotonic() - started)
		if not result.get("skipped") and not (fetch_only or getattr(mapping, "fetch_only", False)):
			phase = "sync"
			remote_ids = None
			if getattr(mapping, "full_snapshot_sync", False) and not cint(getattr(mapping, "fetch_limit", 0)):
				prefix = getattr(mapping, "sap_id_prefix", None) or ""
				remote_ids = set(result.get("success") or [])
				remote_ids.update(f"{prefix}{remote_id}" for remote_id in result.get("excluded") or [])
			result["sync"] = (
				_sync_mapping_and_reconcile(row, remote_ids=remote_ids)
				if remote_ids is not None else _sync_mapping_and_reconcile(row)
			)
		elif fetch_only or getattr(mapping, "fetch_only", False):
			result["fetch_only"] = True
	except Exception:
		traceback = frappe.get_traceback(with_context=False)
		frappe.db.rollback()
		if phase == "fetch":
			_log_fetch(row, "Failed", elapsed=monotonic() - started, traceback=traceback)
		else:
			_log_document_sync_failed(row, traceback)
			if continue_on_sync_error:
				result["sync"] = {"status": "failed"}
				return result
		raise
	finally:
		frappe.cache().delete(lock_key)
	return result


def synchronize_sap_doctype(
	doctype_name: str, configuration_name: str | None = None, *, fetch_only: bool = False,
	from_date: str | None = None, to_date: str | None = None,
) -> dict:
	"""Run enabled SAP mappings for one ERPNext DocType."""
	if bool(from_date) != bool(to_date):
		raise frappe.ValidationError("from_date and to_date must be provided together.")
	mapping_names = frappe.get_all(
		"SAP Doctype Mapping",
		filters={"erp_doctype": doctype_name, "enabled": 1},
		pluck="name",
		limit_page_length=0,
	)
	if not mapping_names:
		raise frappe.ValidationError(f"No enabled SAP mapping targets {doctype_name}.")

	filters = {
		"parenttype": "External SAP Configuration",
		"parentfield": "mappings",
		"sap_mapping": ["in", mapping_names],
	}
	if configuration_name:
		filters["parent"] = configuration_name
	rows = frappe.get_all(
		"External Site Configuration CT",
		filters=filters,
		fields=["name", "parent", "sap_mapping"],
		order_by="idx asc",
		limit_page_length=0,
	)
	if not rows:
		raise frappe.ValidationError(f"No configured SAP mapping targets {doctype_name}.")
	configurations = {row.parent for row in rows}
	if len(configurations) > 1:
		raise frappe.ValidationError("Specify configuration_name to select one SAP configuration.")

	results = []
	for row in _ordered_mapping_rows(rows):
		options = {"from_date": from_date, "to_date": to_date} if from_date else {}
		result = synchronize_sap_data(row.name, fetch_only=fetch_only, **options)
		results.append({
			"mapping": row.sap_mapping,
			"fetched": len(result.get("success") or []),
			"created": len(result.get("created") or []),
			"updated": len(result.get("updated") or []),
			"unchanged": len(result.get("unchanged") or []),
			"failed": len(result.get("failed") or []),
			"skipped": bool(result.get("skipped")),
			"sync": result.get("sync"),
		})
	return {
		"doctype": doctype_name,
		"configuration": next(iter(configurations)),
		"fetch_only": fetch_only,
		"results": results,
	}


def synchronize_sap_configurations(configuration_names: list[str]) -> dict:
	results = [synchronize_sap_configuration(name) for name in configuration_names]
	statuses = {result.get("status") for result in results}
	return {
		"status": (
			"completed_with_errors" if "completed_with_errors" in statuses else
			"completed_with_skips" if "completed_with_skips" in statuses else
			"complete"
		),
		"processed_configurations": len(results),
		"results": results,
	}


def fetch_sap_review_configuration(configuration_name: str) -> dict:
	rows = frappe.get_all(
		"External Site Configuration CT",
		filters={
			"parent": configuration_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
			"sap_mapping": ["is", "set"],
		},
		fields=["name", "sap_mapping"],
		order_by="idx asc",
		limit_page_length=0,
	)
	review_mappings = set(frappe.get_all(
		"SAP Doctype Mapping", filters={"enabled": 1, "fetch_only": 1}, pluck="name"
	))
	results = []
	for row in rows:
		if row.sap_mapping not in review_mappings:
			continue
		result = synchronize_sap_data(row.name)
		results.append({
			"mapping": row.sap_mapping,
			"created": len(result.get("created") or []),
			"updated": len(result.get("updated") or []),
			"unchanged": len(result.get("unchanged") or []),
			"skipped": bool(result.get("skipped")),
		})
	return {"status": "complete", "configuration": configuration_name, "results": results}


def synchronize_sap_configuration(configuration_name: str, *, fetch_only: bool = False) -> dict:
	_log_configuration_started(configuration_name, "Fetch" if fetch_only else "Synchronization")
	if not fetch_only:
		_ensure_company_cost_centers()
	rows = frappe.get_all(
		"External Site Configuration CT",
		filters={
			"parent": configuration_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
			"sap_mapping": ["is", "set"],
		},
		fields=["name", "sap_mapping"],
		order_by="idx asc",
		limit_page_length=0,
	)
	rows = _ordered_mapping_rows(rows)
	enabled_mappings = set(
		frappe.get_all("SAP Doctype Mapping", filters={"enabled": 1}, pluck="name")
	)
	results = []
	logger = frappe.logger("sap_sync")
	logger.setLevel(logging.INFO)
	for row in rows:
		if row.sap_mapping not in enabled_mappings:
			continue
		logger.info("SAP synchronization started: %s / %s", configuration_name, row.sap_mapping)
		result = synchronize_sap_data(
			row.name, continue_on_sync_error=True, fetch_only=fetch_only
		)
		mapping_result = {
			"mapping": row.sap_mapping,
			"status": (
				"skipped" if result.get("skipped") else
				"sync_failed" if (result.get("sync") or {}).get("status") == "failed" else
				"complete"
			),
			"created": len(result.get("created") or []),
			"updated": len(result.get("updated") or []),
			"unchanged": len(result.get("unchanged") or []),
			"excluded": len(result.get("excluded") or []),
			"failed": len(result.get("failed") or []),
			"sync": result.get("sync"),
		}
		results.append(mapping_result)
		logger.info(
			"SAP synchronization %s: %s / %s (created=%s, updated=%s, unchanged=%s, excluded=%s, failed=%s)",
			mapping_result["status"], configuration_name, row.sap_mapping,
			mapping_result["created"], mapping_result["updated"],
			mapping_result["unchanged"], mapping_result["excluded"],
			mapping_result["failed"],
		)
	skipped = sum(item["status"] == "skipped" for item in results)
	failed = sum(item["status"] == "sync_failed" for item in results)
	result = {
		"status": "completed_with_errors" if failed else "completed_with_skips" if skipped else "complete",
		"configuration": configuration_name,
		"processed_mappings": len(results),
		"results": results,
	}
	frappe.log_error(
		title=f"SAP Synchronization Completed: {configuration_name}"[:140],
		message=frappe.as_json(result),
		reference_doctype="External SAP Configuration",
		reference_name=configuration_name,
	)
	frappe.db.commit()
	return result


def _ensure_company_cost_centers() -> None:
	from emkan_insights.patches.v0_0_1.create_sap_default_cost_centers import execute as default_cost_centers
	from emkan_insights.patches.v0_0_1.create_sap_root_cost_centers import execute as root_cost_centers

	default_cost_centers()
	root_cost_centers()
	frappe.db.commit()


def _has_staged_mapping_errors(mapping, configuration: str) -> bool:
	if not frappe.db.table_exists(mapping.external_doctype):
		return False
	# Plain conditions let MariaDB use the configuration index; get_value wraps columns in
	# IFNULL, which turned this check into a scan of the whole staging table.
	table = frappe.qb.DocType(mapping.external_doctype)
	return bool(
		frappe.qb.from_(table).select(table.name)
		.where((table.external_sap_configuration == configuration) & table.json.like("%_sap_mapping_errors%"))
		.limit(1)
	.run())


def _has_empty_staging(mapping, configuration: str) -> bool:
	return frappe.db.table_exists(mapping.external_doctype) and not frappe.db.exists(
		mapping.external_doctype,
		{"external_sap_configuration": configuration},
	)


def sync_sap_configuration(configuration_name: str) -> dict:
	_log_configuration_started(configuration_name, "Sync")
	_ensure_company_cost_centers()
	rows = frappe.get_all(
		"External Site Configuration CT",
		filters={
			"parent": configuration_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
			"sap_mapping": ["is", "set"],
		},
		fields=["name", "sap_mapping"],
		order_by="idx asc",
		limit_page_length=0,
	)
	rows = _ordered_mapping_rows(rows)
	enabled_mappings = set(
		frappe.get_all("SAP Doctype Mapping", filters={"enabled": 1, "fetch_only": 0}, pluck="name")
	)
	results = []
	for row in rows:
		if row.sap_mapping not in enabled_mappings:
			continue
		results.append(sync_sap_data(row.name))
	result = {
		"status": "complete",
		"configuration": configuration_name,
		"processed_mappings": len(results),
		"results": results,
	}
	frappe.log_error(
		title=f"SAP Sync Completed: {configuration_name}"[:140],
		message=frappe.as_json(result),
		reference_doctype="External SAP Configuration",
		reference_name=configuration_name,
		defer_insert=True,
	)
	return result


def _ordered_mapping_rows(rows: list) -> list:
	return [
		row for _, row in sorted(
			enumerate(rows),
			key=lambda pair: mapping_sort_key(pair[1].sap_mapping, pair[0]),
		)
	]


def sync_sap_data(row_name: str) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")

	lock_key = _mapping_lock_key(row.name)
	if not frappe.cache().set(lock_key, "1", nx=True, ex=FETCH_LOCK_TTL):
		return {"status": "skipped", "reason": "A fetch or sync is already running."}

	try:
		return _sync_mapping_and_reconcile(row)
	except Exception:
		traceback = frappe.get_traceback(with_context=False)
		frappe.db.rollback()
		_log_document_sync_failed(row, traceback)
		raise
	finally:
		frappe.cache().delete(lock_key)


def _sync_mapping_and_reconcile(
	row, *, reconcile_deletions: bool = True, remote_ids: set[str] | None = None
) -> dict:
	mapping = frappe.get_doc("SAP Doctype Mapping", row.sap_mapping)
	if getattr(mapping, "fetch_only", False):
		raise frappe.ValidationError(f"SAP mapping {mapping.name} is fetch only.")
	result = sync_mapping(row.name, remote_ids=remote_ids)
	if mapping.name == "SAP Fixed Asset":
		from emkan_insights.sap.asset_acquisition import (
			reconcile_asset_acquisitions,
			reconcile_asset_retirements,
			submit_imported_assets,
		)

		result["asset_acquisition"] = reconcile_asset_acquisitions(row.parent, apply=True)
		submit_rule = frappe.parse_json(mapping.field_mapping).get("docstatus")
		if (
			submit_rule.get("fieldname") == "AssetStatus"
			and submit_rule.get("transform") == "value_map"
			and submit_rule.get("transform_config", {}).get("mapping", {}).get("Active") == 1
		):
			result["asset_submission"] = submit_imported_assets(row.parent, apply=True)
		result["asset_retirement"] = reconcile_asset_retirements(row.parent, apply=True)
	if reconcile_deletions and mapping.full_snapshot_sync and not cint(getattr(mapping, "fetch_limit", 0)):
		from emkan_insights.sap.deletion import reconcile_mapping_deletions

		if remote_ids is None:
			result["deletion_reconciliation"] = reconcile_mapping_deletions(row.name)
		else:
			result["deletion_reconciliation"] = reconcile_mapping_deletions(row.name, remote_ids=remote_ids)
	_log_document_sync_completed(row, result)
	return result


def _log_configuration_started(configuration_name: str, operation: str) -> None:
	frappe.log_error(
		title=f"SAP {operation} Job Started: {configuration_name}"[:140],
		message=f"Status: Started\nConfiguration: {configuration_name}",
		reference_doctype="External SAP Configuration",
		reference_name=configuration_name,
	)
	frappe.db.commit()


def _log_document_sync_completed(row, result: dict) -> None:
	target_doctype = result.get("target_doctype") or getattr(row, "ref_doctype", None) or row.sap_mapping
	reconciliation = result.get("deletion_reconciliation") or {}
	frappe.log_error(
		title=f"SAP Document Sync Completed: {target_doctype}"[:140],
		message="\n".join([
			"Status: Completed",
			f"Configuration: {row.parent}",
			f"Mapping: {row.sap_mapping}",
			f"ERPNext DocType: {target_doctype}",
			f"Inserted: {result.get('inserted', 0)}",
			f"Updated: {result.get('updated', 0)}",
			f"Skipped: {result.get('skipped', 0)}",
			f"Deletion Reconciliation: {reconciliation.get('status', 'Not required')}",
		]),
		reference_doctype="External SAP Configuration",
		reference_name=row.parent,
	)
	frappe.db.commit()


def _log_document_sync_failed(row, traceback: str) -> None:
	frappe.log_error(
		title=f"SAP Document Sync Failed: {getattr(row, 'ref_doctype', None) or row.sap_mapping}"[:140],
		message="\n".join([
			"Status: Failed",
			f"Configuration: {row.parent}",
			f"Mapping: {row.sap_mapping}",
			f"ERPNext DocType: {getattr(row, 'ref_doctype', None) or row.sap_mapping}",
			f"Staging DocType: {getattr(row, 'exported_doctype', None) or 'Not specified'}",
			f"Mapping row: {row.name}",
			traceback,
		]),
		reference_doctype="External SAP Configuration",
		reference_name=row.parent,
	)
	frappe.db.commit()


def _log_fetch(row, status, result=None, elapsed=None, traceback=None):
	result = result or {}
	message = [
		f"Status: {status}",
		f"Configuration: {row.parent}",
		f"Mapping: {row.sap_mapping}",
		f"ERPNext DocType: {getattr(row, 'ref_doctype', None) or row.sap_mapping}",
		f"Staging DocType: {getattr(row, 'exported_doctype', None) or 'Not specified'}",
		f"Mapping row: {row.name}",
	]
	if status.startswith("Completed"):
		created, updated, unchanged, excluded, failed = (
			len(result.get(key) or []) for key in ("created", "updated", "unchanged", "excluded", "failed")
		)
		message.extend([
			f"Processed: {created + updated + unchanged + excluded + failed}",
			f"Created: {created}", f"Updated: {updated}",
			f"Unchanged: {unchanged}", f"Excluded: {excluded}", f"Failed: {failed}",
			f"Last successful fetch: {result.get('last_sync') or 'Not advanced'}",
		])
	if result.get("reason"):
		message.append(f"Reason: {result['reason']}")
	if elapsed is not None:
		message.append(f"Duration: {elapsed:.2f} seconds")
	if traceback:
		message.append(traceback)
	frappe.log_error(
		title=f"SAP Fetch {status}: {row.sap_mapping}"[:140],
		message="\n".join(message),
		reference_doctype="External SAP Configuration",
		reference_name=row.parent,
	)
	frappe.db.commit()


def stop_configuration_synchronization(configuration_name: str) -> dict:
	"""Stop this company's SAP background jobs and clear its mapping locks.

	A job stopped by force never reaches its `finally` block, so its mapping lock stays in
	Redis until the two-hour expiry and every later run of that mapping is skipped.
	Jobs that also cover other companies are left running and reported.
	"""
	from frappe.utils.background_jobs import get_queues, get_redis_conn
	from rq.command import send_stop_job_command
	from rq.exceptions import NoSuchJobError
	from rq.job import Job
	from rq.registry import StartedJobRegistry

	row_names = frappe.get_all(
		"External Site Configuration CT",
		filters={
			"parent": configuration_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
		},
		pluck="name",
		limit_page_length=0,
	)
	connection = get_redis_conn()
	result = {"stopped": [], "cancelled": [], "left_running": [], "locks_cleared": 0}
	seen = set()
	for queue in get_queues():
		registry = StartedJobRegistry(queue=queue)
		for job_id in [*queue.job_ids, *registry.get_job_ids()]:
			if job_id in seen:
				continue
			seen.add(job_id)
			try:
				job = Job.fetch(job_id, connection=connection)
			except NoSuchJobError:
				continue
			if job.kwargs.get("site") != frappe.local.site:
				continue
			configurations = _job_configurations(job)
			if configuration_name not in configurations:
				continue
			label = job_id.split("::", 1)[-1]
			if configurations != {configuration_name}:
				result["left_running"].append(label)
				continue
			status = job.get_status()
			if status == "queued":
				job.cancel()
				result["cancelled"].append(label)
			elif status == "started":
				try:
					send_stop_job_command(connection, job.id)
				except Exception:
					# The worker is gone, so the job is stale: drop it from the started registry.
					registry.remove(job, delete_job=True)
				result["stopped"].append(label)

	for row_name in row_names:
		result["locks_cleared"] += int(frappe.cache().delete(_mapping_lock_key(row_name)) or 0)
	return result


def _job_configurations(job) -> set[str]:
	"""External SAP Configurations a queued or running SAP job works on."""
	arguments = job.kwargs.get("kwargs") or {}
	configurations = set()
	if arguments.get("configuration_name"):
		configurations.add(arguments["configuration_name"])
	configurations.update(arguments.get("configuration_names") or [])
	if arguments.get("row_name"):
		parent = frappe.db.get_value("External Site Configuration CT", arguments["row_name"], "parent")
		if parent:
			configurations.add(parent)
	return configurations


def _mapping_lock_key(row_name: str) -> str:
	# Sites in one bench share Redis and can hold identically named mapping rows.
	return f"sap_master_sync:{frappe.local.site}:{row_name}"


def _job_id(row_name: str) -> str:
	digest = hashlib.sha1(row_name.encode(), usedforsecurity=False).hexdigest()[:12]
	return f"sap_fetch_data_{digest}"


def _configuration_sync_job_id(configuration_name: str) -> str:
	digest = hashlib.sha1(configuration_name.encode(), usedforsecurity=False).hexdigest()[:12]
	return f"sap_sync_data_{digest}"


def _configuration_synchronization_job_id(configuration_name: str) -> str:
	digest = hashlib.sha1(configuration_name.encode(), usedforsecurity=False).hexdigest()[:12]
	return f"sap_synchronize_{digest}"


# Existing queued jobs and scheduler records can still resolve their old paths.
fetch_sap_data = synchronize_sap_data
sync_mapping_row = synchronize_sap_data
enqueue_incremental_master_fetch = enqueue_sap_synchronization
enqueue_incremental_master_sync = enqueue_sap_synchronization
