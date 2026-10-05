from __future__ import annotations

import frappe
from frappe.utils.nestedset import rebuild_tree

from emkan_insights.emkan_insights.external_data_bulk_insert import PAGE_SIZE, add_prefix
from emkan_insights.emkan_insights.external_data_deletion import _delete_rows_with_children
from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.services.fetch import fetch
from emkan_insights.sap.sync import SAPSyncContext, get_sync_context


class SAPDeletionError(frappe.ValidationError):
	pass


def reconcile_mapping_deletions(row_name: str) -> dict:
	"""Delete staged and target rows that no longer exist in SAP."""
	context = get_sync_context(row_name)
	mapping = frappe.get_doc("SAP Doctype Mapping", context.mapping_name)
	client = SAPB1Client.from_configuration(context.configuration)
	remote_ids = _fetch_remote_ids(client, mapping)
	staging_by_remote_id = _get_staging_rows(context)
	_validate_snapshot(context, remote_ids, staging_by_remote_id)
	missing_ids = set(staging_by_remote_id) - remote_ids

	result = {
		"status": "complete",
		"mapping": context.mapping_name,
		"remote": len(remote_ids),
		"staging": len(staging_by_remote_id),
		"missing": len(missing_ids),
		"deleted": 0,
		"already_missing": 0,
		"failed": 0,
		"errors": [],
	}
	for remote_id_batch in _batches(_order_missing_ids(context, missing_ids)):
		_reconcile_batch(context, remote_id_batch, staging_by_remote_id, result)

	if missing_ids:
		_rebuild_tree(context.target_doctype)
		frappe.db.commit()
	_log_result(context, result)
	return result


def _fetch_remote_ids(client: SAPB1Client, mapping) -> set[str]:
	records = fetch(client, mapping)
	remote_ids = set()
	for record in records:
		remote_id = record.get(mapping.sap_id_field)
		if remote_id in (None, ""):
			raise SAPDeletionError(
				f"SAP {mapping.sap_doctype} returned a row without {mapping.sap_id_field}."
			)
		remote_ids.add(str(remote_id))
	return remote_ids


def _get_staging_rows(context: SAPSyncContext) -> dict[str, list[str]]:
	source = frappe.qb.DocType(context.source_doctype)
	rows = (
		frappe.qb.from_(source)
		.select(source.name, source.remote_id)
		.where(
			(source.external_sap_configuration == context.configuration)
			& source.remote_id.notnull()
			& (source.remote_id != "")
		)
	).run(as_dict=True)
	result = {}
	for row in rows:
		result.setdefault(str(row.remote_id), []).append(row.name)
	return result


def _validate_snapshot(
	context: SAPSyncContext,
	remote_ids: set[str],
	staging_by_remote_id: dict[str, list[str]],
) -> None:
	if staging_by_remote_id and not remote_ids:
		raise SAPDeletionError(
			f"SAP {context.mapping_name} returned an empty snapshot while "
			f"{len(staging_by_remote_id)} staged record(s) exist."
		)


def _reconcile_batch(
	context: SAPSyncContext,
	remote_ids: list[str],
	staging_by_remote_id: dict[str, list[str]],
	result: dict,
) -> None:
	target_names = _get_target_names(context, remote_ids)
	staging_names = [name for remote_id in remote_ids for name in staging_by_remote_id[remote_id]]
	savepoint = f"sap_delete_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_delete_rows_with_children(context.target_doctype, target_names)
		_delete_rows_with_children(context.source_doctype, staging_names)
		result["deleted"] += len(target_names)
		result["already_missing"] += len(remote_ids) - len(target_names)
		frappe.db.commit()
	except Exception as error:
		frappe.db.rollback(save_point=savepoint)
		result["failed"] += len(remote_ids)
		result["errors"].append({"remote_ids": remote_ids, "error": str(error)})


def _get_target_names(context: SAPSyncContext, remote_ids: list[str]) -> list[str]:
	if context.erp_sap_id_fieldname:
		return frappe.get_all(
			context.target_doctype,
			filters={context.erp_sap_id_fieldname: ["in", remote_ids]},
			pluck="name",
			limit_page_length=0,
		)

	names = [
		add_prefix(remote_id, context.bulk.company_abbr)
		if context.target_doctype in context.bulk.abbr_doctypes
		else remote_id
		for remote_id in remote_ids
	]
	return frappe.get_all(
		context.target_doctype,
		filters={"name": ["in", names]},
		pluck="name",
		limit_page_length=0,
	)


def _order_missing_ids(context: SAPSyncContext, remote_ids: set[str]) -> list[str]:
	ordered = sorted(remote_ids)
	if not ordered or not frappe.get_meta(context.target_doctype).is_tree:
		return ordered

	target_to_remote = {}
	for remote_id_batch in _batches(ordered):
		for target_name in _get_target_names(context, remote_id_batch):
			target_to_remote[target_name] = _remote_id_for_target(context, target_name, remote_id_batch)
	rows = frappe.get_all(
		context.target_doctype,
		filters={"name": ["in", list(target_to_remote)]},
		fields=["name", "lft"],
		limit_page_length=0,
	)
	lft_by_remote = {target_to_remote[row.name]: row.lft or 0 for row in rows}
	return sorted(ordered, key=lambda remote_id: lft_by_remote.get(remote_id, 0), reverse=True)


def _remote_id_for_target(
	context: SAPSyncContext,
	target_name: str,
	remote_ids: list[str],
) -> str:
	if context.erp_sap_id_fieldname:
		value = frappe.db.get_value(
			context.target_doctype,
			target_name,
			context.erp_sap_id_fieldname,
		)
		return str(value)
	for remote_id in remote_ids:
		name = (
			add_prefix(remote_id, context.bulk.company_abbr)
			if context.target_doctype in context.bulk.abbr_doctypes
			else remote_id
		)
		if name == target_name:
			return remote_id
	return target_name


def _rebuild_tree(doctype: str) -> None:
	if frappe.get_meta(doctype).is_tree:
		rebuild_tree(doctype)


def _batches(values: list[str]):
	for start in range(0, len(values), PAGE_SIZE):
		yield values[start : start + PAGE_SIZE]


def _log_result(context: SAPSyncContext, result: dict) -> None:
	status = "Completed with failures" if result["failed"] else "Completed"
	frappe.log_error(
		title=f"SAP Deletion {status}: {context.mapping_name}"[:140],
		message=(
			f"Configuration: {context.configuration}\n"
			f"Mapping: {context.mapping_name}\n"
			f"Remote: {result['remote']}\nStaging: {result['staging']}\n"
			f"Missing: {result['missing']}\nDeleted: {result['deleted']}\n"
			f"Already missing: {result['already_missing']}\nFailed: {result['failed']}\n"
			f"Errors: {frappe.as_json(result['errors'])}"
		),
		reference_doctype="External SAP Configuration",
		reference_name=context.configuration,
		defer_insert=True,
	)
