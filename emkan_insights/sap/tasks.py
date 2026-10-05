from __future__ import annotations

import hashlib
from time import monotonic

import frappe

from emkan_insights.sap.services import get_mapped_data
from emkan_insights.sap.sync import sync_mapping


FETCH_LOCK_TTL = 2 * 60 * 60


def enqueue_incremental_master_fetch() -> dict[str, int]:
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
	queued = 0
	for row in rows:
		if row.sap_mapping not in enabled_mappings:
			continue
		frappe.enqueue(
			"emkan_insights.sap.tasks.fetch_sap_data",
			queue="long",
			timeout=FETCH_LOCK_TTL,
			job_id=_job_id(row.name),
			deduplicate=True,
			row_name=row.name,
		)
		queued += 1
	return {"queued": queued}


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


def fetch_sap_data(row_name: str) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")

	# Share the lock with workers already running the previous job name.
	lock_key = f"sap_master_sync:{row.name}"
	if not frappe.cache().set(lock_key, "1", nx=True, ex=FETCH_LOCK_TTL):
		result = {"skipped": True, "reason": "A fetch is already running for this mapping."}
		_log_fetch(row, "Skipped", result=result)
		return result
	started = monotonic()
	try:
		_log_fetch(row, "Started")
		result = get_mapped_data(
			row.sap_mapping,
			configuration_name=row.parent,
			row_name=row.name,
		)
		status = "Skipped" if result.get("skipped") else "Completed"
		if result.get("failed"):
			status = "Completed with failures"
		_log_fetch(row, status, result=result, elapsed=monotonic() - started)
		return result
	except Exception:
		_log_fetch(
			row, "Failed", elapsed=monotonic() - started,
			traceback=frappe.get_traceback(with_context=False),
		)
		raise
	finally:
		frappe.cache().delete(lock_key)


def sync_sap_configuration(configuration_name: str) -> dict:
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
	enabled_mappings = set(
		frappe.get_all("SAP Doctype Mapping", filters={"enabled": 1}, pluck="name")
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


def sync_sap_data(row_name: str) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")

	lock_key = f"sap_master_sync:{row.name}"
	if not frappe.cache().set(lock_key, "1", nx=True, ex=FETCH_LOCK_TTL):
		return {"status": "skipped", "reason": "A fetch or sync is already running."}

	try:
		result = sync_mapping(row.name)
		from emkan_insights.sap.deletion import reconcile_mapping_deletions

		result["deletion_reconciliation"] = reconcile_mapping_deletions(row.name)
		return result
	except Exception:
		frappe.log_error(
			title=f"SAP Sync Failed: {row.sap_mapping}"[:140],
			message=frappe.get_traceback(with_context=False),
			reference_doctype="External SAP Configuration",
			reference_name=row.parent,
			defer_insert=True,
		)
		raise
	finally:
		frappe.cache().delete(lock_key)


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
		created, updated, failed = (len(result.get(key) or []) for key in ("created", "updated", "failed"))
		message.extend([
			f"Processed: {created + updated + failed}",
			f"Created: {created}", f"Updated: {updated}", f"Failed: {failed}",
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
		defer_insert=True,
	)


def _job_id(row_name: str) -> str:
	digest = hashlib.sha1(row_name.encode(), usedforsecurity=False).hexdigest()[:12]
	return f"sap_fetch_data_{digest}"


def _configuration_sync_job_id(configuration_name: str) -> str:
	digest = hashlib.sha1(configuration_name.encode(), usedforsecurity=False).hexdigest()[:12]
	return f"sap_sync_data_{digest}"


# Existing queued jobs and scheduler records can still resolve their old paths.
sync_mapping_row = fetch_sap_data
enqueue_incremental_master_sync = enqueue_incremental_master_fetch
