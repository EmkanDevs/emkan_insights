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
	row_name: str, *, continue_on_sync_error: bool = False, fetch_only: bool = False
) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")

	# Share the lock with workers already running the previous job name.
	lock_key = f"sap_master_sync:{row.name}"
	if not frappe.cache().set(lock_key, "1", nx=True, ex=FETCH_LOCK_TTL):
		result = {"skipped": True, "reason": "A synchronization is already running for this mapping."}
		_log_fetch(row, "Skipped", result=result)
		return result
	started = monotonic()
	phase = "fetch"
	try:
		_log_fetch(row, "Started")
		mapping = frappe.get_doc("SAP Doctype Mapping", row.sap_mapping)
		fetch_options = {}
		if not mapping.full_snapshot_sync and _has_staged_mapping_errors(mapping, row.parent):
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
	if not fetch_only:
		_ensure_company_roots()
	_log_configuration_started(configuration_name, "Fetch" if fetch_only else "Synchronization")
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


def _ensure_company_roots() -> None:
	from emkan_insights.patches.v0_0_1.create_sap_company_account_roots import execute as account_roots
	from emkan_insights.patches.v0_0_1.create_sap_default_cost_centers import execute as default_cost_centers
	from emkan_insights.patches.v0_0_1.create_sap_root_cost_centers import execute as root_cost_centers

	account_roots()
	default_cost_centers()
	root_cost_centers()
	frappe.db.commit()


def _has_staged_mapping_errors(mapping, configuration: str) -> bool:
	if not frappe.db.table_exists(mapping.external_doctype):
		return False
	return bool(frappe.db.get_value(
		mapping.external_doctype,
		{"external_sap_configuration": configuration, "json": ["like", "%_sap_mapping_errors%"]},
		"name",
	))


def sync_sap_configuration(configuration_name: str) -> dict:
	_ensure_company_roots()
	_log_configuration_started(configuration_name, "Sync")
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

	lock_key = f"sap_master_sync:{row.name}"
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
