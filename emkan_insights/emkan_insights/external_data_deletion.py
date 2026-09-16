from dataclasses import dataclass
from urllib.parse import quote

import frappe
import requests
from frappe import _
from frappe.utils.nestedset import rebuild_tree

from emkan_insights.emkan_insights.external_data_bulk_insert import (
	BULK_INSERT_LOCK_TTL,
	BulkInsertContext,
	_get_context as _get_bulk_insert_context,
	_lock_key,
	_ordered_configuration_rows,
	_source_site_condition,
	add_prefix,
)
from emkan_insights.emkan_insights.external_data_fetch import DEFERRED_NAMING_DOCTYPES


PAGE_SIZE = 500
DELETION_BATCH_JOB_ID = "external_data_deletion_reconciliation"
DELETION_LOCK_KEY = "external_data_deletion_lock"
DELETION_LOCK_TTL = 12 * 60 * 60


@dataclass(frozen=True)
class DeletionContext:
	configuration: str
	remote_doctype: str
	staging_doctype: str
	target_doctype: str
	site_url: str
	api_key: str
	api_secret: str
	company_abbr: str
	abbr_doctypes: frozenset[str]
	source_site_url: str = ""


@frappe.whitelist(methods=["POST"])
def queue_external_data_deletion_reconciliation(configuration_name: str) -> dict:
	"""Queue a full remote-name reconciliation for one external site."""
	configuration = frappe.get_doc("External Site Configuration", configuration_name)
	frappe.has_permission(
		"External Site Configuration",
		"write",
		doc=configuration.name,
		throw=True,
	)
	return enqueue_external_data_deletion_reconciliation(configuration.name)


def enqueue_external_data_deletion_reconciliation(
	configuration_name: str,
	*,
	enqueue_after_commit: bool = True,
) -> dict:
	"""Enqueue reconciliation after callers have released the pipeline lock."""
	job = frappe.enqueue(
		"emkan_insights.emkan_insights.reconcile_missing_records.execute",
		queue="long",
		timeout=DELETION_LOCK_TTL,
		enqueue_after_commit=enqueue_after_commit,
		job_id=f"external_data_deletion_{configuration_name}",
		deduplicate=True,
		configuration_name=configuration_name,
		doctype=None,
		dry_run=False,
	)
	return {
		"status": "queued" if job else "already_queued",
		"configuration": configuration_name,
		"job_id": f"external_data_deletion_{configuration_name}",
	}


def enqueue_external_data_deletion_batch(
	configuration_names: list[str],
	*,
	enqueue_after_commit: bool = True,
) -> dict:
	"""Queue one job that reconciles configurations sequentially."""
	configuration_names = list(dict.fromkeys(configuration_names))
	if not configuration_names:
		return {"status": "skipped", "configurations": 0}

	job = frappe.enqueue(
		"emkan_insights.emkan_insights.external_data_deletion.reconcile_external_data_deletion_batch",
		queue="long",
		timeout=DELETION_LOCK_TTL,
		enqueue_after_commit=enqueue_after_commit,
		job_id=DELETION_BATCH_JOB_ID,
		deduplicate=True,
		configuration_names=configuration_names,
	)
	return {
		"status": "queued" if job else "already_queued",
		"configurations": len(configuration_names),
		"job_id": DELETION_BATCH_JOB_ID,
	}


def reconcile_external_data_deletion_batch(configuration_names: list[str]) -> dict:
	"""Reconcile multiple configurations serially under one deletion lock."""
	from emkan_insights.emkan_insights.reconcile_missing_records import execute

	results = []
	with _deletion_lock():
		for configuration_name in configuration_names:
			try:
				result = execute(
					configuration_name,
					doctype=None,
					dry_run=False,
					deletion_lock_acquired=True,
				)
			except Exception:
				frappe.db.rollback()
				traceback = frappe.get_traceback()
				frappe.log_error(
					title=f"External deletion failed: {configuration_name}"[:140],
					message=traceback,
				)
				result = {
					"status": "failed",
					"configuration": configuration_name,
					"error": traceback,
				}
			results.append(result)

	failed = sum(result["status"] != "complete" for result in results)
	return {
		"status": "complete_with_errors" if failed else "complete",
		"configurations": len(configuration_names),
		"failed": failed,
		"results": results,
	}


def reconcile_external_data_deletions(
	configuration_name: str,
	*,
	lock_acquired: bool = False,
	deletion_lock_acquired: bool = False,
) -> dict:
	"""Delete targets and staging rows whose remote records no longer exist."""
	if not deletion_lock_acquired:
		with _deletion_lock():
			return reconcile_external_data_deletions(
				configuration_name,
				lock_acquired=lock_acquired,
				deletion_lock_acquired=True,
			)

	acquired_here = False
	if not lock_acquired:
		acquired_here = bool(
			frappe.cache().set(
				_lock_key(configuration_name),
				"1",
				nx=True,
				ex=BULK_INSERT_LOCK_TTL,
			)
		)
		if not acquired_here:
			return {
				"status": "skipped",
				"configuration": configuration_name,
				"message": _("Another fetch or sync is running for this configuration."),
			}

	try:
		configuration = frappe.get_doc("External Site Configuration", configuration_name)
		results = []
		for row in reversed(_ordered_configuration_rows(configuration)):
			context = _get_context(row.name)
			results.append(_reconcile_doctype_deletions(context))

		failed = sum(result["failed"] for result in results)
		blocked = sum(result["blocked"] for result in results)
		result = {
			"status": "complete_with_errors" if failed or blocked else "complete",
			"configuration": configuration.name,
			"processed_doctypes": len(results),
			"deleted": sum(item["deleted"] for item in results),
			"already_missing": sum(item["already_missing"] for item in results),
			"blocked": blocked,
			"failed": failed,
			"results": results,
		}
		frappe.logger("external_data_deletion").info(
			"External deletion reconciliation complete: %s",
			result,
		)
		return result
	finally:
		if acquired_here:
			frappe.cache().delete(_lock_key(configuration_name))


def _deletion_lock():
	cache = frappe.cache()
	return cache.lock(
		cache.make_key(DELETION_LOCK_KEY),
		timeout=DELETION_LOCK_TTL,
		blocking_timeout=DELETION_LOCK_TTL,
	)


def _get_context(child_docname: str) -> DeletionContext:
	bulk_context = _get_bulk_insert_context(child_docname)
	configuration = frappe.get_doc(
		"External Site Configuration",
		bulk_context.configuration,
	)
	site_url = (configuration.site_url or "").rstrip("/")
	if not site_url or not configuration.api_key or not configuration.api_secret:
		frappe.throw(
			_("External Site Configuration {0} has incomplete connection details.").format(
				configuration.name
			)
		)

	return DeletionContext(
		configuration=configuration.name,
		remote_doctype=bulk_context.target_doctype,
		staging_doctype=bulk_context.source_doctype,
		target_doctype=bulk_context.target_doctype,
		site_url=site_url,
		api_key=configuration.api_key,
		api_secret=configuration.api_secret,
		company_abbr=bulk_context.company_abbr,
		abbr_doctypes=bulk_context.abbr_doctypes,
		source_site_url=bulk_context.source_site_url,
	)


def _reconcile_doctype_deletions(context: DeletionContext) -> dict:
	remote_ids = _fetch_all_remote_ids(context)
	staging_by_remote_id = _get_staging_rows(context)
	_validate_remote_snapshot(context, remote_ids, staging_by_remote_id)
	missing_remote_ids = set(staging_by_remote_id) - remote_ids
	result = _empty_reconciliation_result(context, remote_ids, staging_by_remote_id, missing_remote_ids)

	ordered_remote_ids = _order_missing_remote_ids(context, missing_remote_ids)
	for remote_id_batch in _get_batches(ordered_remote_ids):
		batch_result = _reconcile_missing_batch(context, remote_id_batch, staging_by_remote_id)
		_merge_reconciliation_result(result, batch_result)

	if missing_remote_ids:
		_rebuild_tree_if_needed(context.target_doctype)
		_rebuild_tree_if_needed(context.staging_doctype)
		frappe.db.commit()

	_log_reconciliation_errors(context, result)
	return result


def _validate_remote_snapshot(
	context: DeletionContext,
	remote_ids: set[str],
	staging_by_remote_id: dict[str, list[str]],
) -> None:
	if staging_by_remote_id and not remote_ids:
		raise ExternalDataDeletionError(
			f"{context.remote_doctype} returned an empty snapshot while "
			f"{len(staging_by_remote_id)} staged remote record(s) exist"
		)


def _empty_reconciliation_result(
	context: DeletionContext,
	remote_ids: set[str],
	staging_by_remote_id: dict[str, list[str]],
	missing_remote_ids: set[str],
) -> dict:
	return {
		"remote_doctype": context.remote_doctype,
		"staging_doctype": context.staging_doctype,
		"target_doctype": context.target_doctype,
		"remote": len(remote_ids),
		"staging": len(staging_by_remote_id),
		"missing": len(missing_remote_ids),
		"deleted": 0,
		"already_missing": 0,
		"blocked": 0,
		"failed": 0,
		"errors": [],
	}


def _reconcile_missing_batch(
	context: DeletionContext,
	remote_ids: list[str],
	staging_by_remote_id: dict[str, list[str]],
) -> dict:
	result = _empty_batch_result()
	target_names = [_get_target_name(context, remote_id) for remote_id in remote_ids]
	existing_target_names = _get_existing_target_names(context.target_doctype, target_names)
	staging_names = _get_staging_names(remote_ids, staging_by_remote_id)
	savepoint = _savepoint_name()
	frappe.db.savepoint(savepoint)

	try:
		_delete_rows_with_children(context.target_doctype, existing_target_names)
		_delete_rows_with_children(context.staging_doctype, staging_names)
		result["deleted"] = len(existing_target_names)
		result["already_missing"] = len(target_names) - len(existing_target_names)
		frappe.db.commit()
	except Exception as error:
		frappe.db.rollback(save_point=savepoint)
		result["failed"] = len(remote_ids)
		result["errors"].append(_error_details(remote_ids, error))

	return result


def _empty_batch_result() -> dict:
	return {"deleted": 0, "already_missing": 0, "blocked": 0, "failed": 0, "errors": []}


def _merge_reconciliation_result(result: dict, batch_result: dict) -> None:
	for fieldname in ("deleted", "already_missing", "blocked", "failed"):
		result[fieldname] += batch_result[fieldname]
	result["errors"].extend(batch_result["errors"])


def _get_staging_names(
	remote_ids: list[str],
	staging_by_remote_id: dict[str, list[str]],
) -> list[str]:
	return [name for remote_id in remote_ids for name in staging_by_remote_id[remote_id]]


def _error_details(remote_ids: list[str], error: Exception, target_name: str | None = None) -> dict:
	return {
		"remote_id": remote_ids[0] if len(remote_ids) == 1 else remote_ids,
		"target_name": target_name,
		"error": str(error),
	}


def _log_reconciliation_errors(context: DeletionContext, result: dict) -> None:
	if not result["errors"]:
		return
	frappe.log_error(
		title=f"External deletion issues: {context.target_doctype}"[:140],
		message=frappe.as_json(result["errors"]),
	)


def _fetch_all_remote_ids(context: DeletionContext) -> set[str]:
	remote_ids = set()
	cursor = None

	while True:
		filters = []
		if context.remote_doctype in DEFERRED_NAMING_DOCTYPES:
			filters.append(["to_rename", "=", 0])
		if cursor:
			filters.append(["name", ">", cursor])

		response = requests.get(
			f"{context.site_url}/api/resource/{quote(context.remote_doctype, safe='')}",
			headers=_headers(context),
			params={
				"fields": frappe.as_json(["name"]),
				"filters": frappe.as_json(filters),
				"limit_page_length": PAGE_SIZE,
				"order_by": "name asc",
			},
			timeout=90,
		)
		response.raise_for_status()
		data = response.json().get("data")
		if not isinstance(data, list):
			raise ExternalDataDeletionError(
				f"{context.remote_doctype} returned an invalid name list"
			)

		page = [str(row.get("name") or "").strip() for row in data]
		if any(not remote_id for remote_id in page):
			raise ExternalDataDeletionError(
				f"{context.remote_doctype} returned a row without a name"
			)
		if page and cursor and page[-1] <= cursor:
			raise ExternalDataDeletionError(
				f"{context.remote_doctype} name pagination did not advance"
			)

		remote_ids.update(page)
		if len(data) < PAGE_SIZE:
			break
		cursor = page[-1]

	return remote_ids


def _get_staging_rows(context: DeletionContext) -> dict[str, list[str]]:
	staging = frappe.qb.DocType(context.staging_doctype)
	bulk_context = _get_bulk_insert_context_for_deletion(context)
	rows = (
		frappe.qb.from_(staging)
		.select(staging.name, staging.remote_id)
		.where(
			_source_site_condition(staging, bulk_context)
			& staging.remote_id.notnull()
			& (staging.remote_id != "")
		)
	).run(as_dict=True)

	staging_by_remote_id = {}
	for row in rows:
		staging_by_remote_id.setdefault(str(row.remote_id), []).append(row.name)
	return staging_by_remote_id


def _get_bulk_insert_context_for_deletion(context: DeletionContext) -> BulkInsertContext:
	return BulkInsertContext(
		configuration=context.configuration,
		source_doctype=context.staging_doctype,
		target_doctype=context.target_doctype,
		company_abbr=context.company_abbr,
		abbr_doctypes=context.abbr_doctypes,
		requested_by=frappe.session.user,
		source_site_url=context.source_site_url,
	)


def _get_target_name(context: DeletionContext, remote_id: str) -> str:
	if context.target_doctype in context.abbr_doctypes:
		return add_prefix(remote_id, context.company_abbr)
	return remote_id


def _get_existing_target_names(doctype: str, names: list[str]) -> list[str]:
	if not names:
		return []
	return frappe.get_all(
		doctype,
		filters={"name": ["in", names]},
		pluck="name",
		limit_page_length=0,
	)


def _order_missing_remote_ids(context: DeletionContext, remote_ids: set[str]) -> list[str]:
	ordered_remote_ids = sorted(remote_ids)
	if not ordered_remote_ids or not frappe.get_meta(context.target_doctype).is_tree:
		return ordered_remote_ids

	target_by_remote_id = {
		remote_id: _get_target_name(context, remote_id) for remote_id in ordered_remote_ids
	}
	lft_by_target = {}
	for target_batch in _get_batches(list(target_by_remote_id.values())):
		rows = frappe.get_all(
			context.target_doctype,
			filters={"name": ["in", target_batch]},
			fields=["name", "lft"],
			limit_page_length=0,
		)
		lft_by_target.update({row.name: row.lft or 0 for row in rows})

	return sorted(
		ordered_remote_ids,
		key=lambda remote_id: lft_by_target.get(target_by_remote_id[remote_id], 0),
		reverse=True,
	)


def _savepoint_name() -> str:
	return f"external_delete_{frappe.generate_hash(length=10)}"


def _delete_rows_with_children(doctype: str, names: list[str]) -> None:
	"""Run direct DELETE queries for child rows and then parent rows."""
	if not names:
		return

	for table_field in frappe.get_meta(doctype).get_table_fields():
		frappe.db.delete(
			table_field.options,
			{"parent": ["in", names], "parenttype": doctype},
		)
		frappe.clear_document_cache(table_field.options)
	frappe.db.delete(doctype, {"name": ["in", names]})
	frappe.clear_document_cache(doctype)


def _rebuild_tree_if_needed(doctype: str) -> None:
	if frappe.get_meta(doctype).is_tree:
		rebuild_tree(doctype)


def _get_batches(values: list[str]):
	for start in range(0, len(values), PAGE_SIZE):
		yield values[start : start + PAGE_SIZE]


def _headers(context: DeletionContext) -> dict:
	return {
		"Authorization": f"token {context.api_key}:{context.api_secret}",
		"Content-Type": "application/json",
	}


class ExternalDataDeletionError(frappe.ValidationError):
	pass
