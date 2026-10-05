from __future__ import annotations

import json
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import now_datetime
from frappe.utils.nestedset import rebuild_tree

from emkan_insights.emkan_insights.external_data_bulk_insert import (
	PAGE_SIZE,
	BulkInsertContext,
	_insert_document_trees,
	_partition_document_trees,
	_prepare_document_tree,
	_update_document_trees,
)


@dataclass(frozen=True)
class SAPSyncContext:
	row_name: str
	configuration: str
	mapping_name: str
	source_doctype: str
	target_doctype: str
	erp_sap_id_fieldname: str | None
	bulk: BulkInsertContext


def sync_mapping(row_name: str) -> dict:
	"""Bulk insert pending SAP staging rows into one ERPNext DocType."""
	context = get_sync_context(row_name)
	started_at = now_datetime()
	_validate_source(context)
	inserted = 0
	updated = 0

	_log(context, "Started", pending=_count_pending_rows(context, started_at))
	while True:
		source_rows = _get_pending_rows(context, started_at)
		if not source_rows:
			break

		target_names = _get_target_names_by_remote_id(context, source_rows)
		documents = [
			_prepare_source_row(context, row, target_names.get(str(row.remote_id)))
			for row in source_rows
		]
		new_documents, existing_documents = _partition_document_trees(
			context.target_doctype,
			documents,
		)
		inserted += _insert_document_trees(new_documents)
		updated += _update_document_trees(context.target_doctype, existing_documents)
		_mark_rows_synced(context.source_doctype, source_rows)
		frappe.db.commit()

	if frappe.get_meta(context.target_doctype).is_tree:
		rebuild_tree(context.target_doctype)
		frappe.db.commit()

	result = {
		"status": "complete",
		"mapping": context.mapping_name,
		"source_doctype": context.source_doctype,
		"target_doctype": context.target_doctype,
		"inserted": inserted,
		"updated": updated,
	}
	_log(context, "Completed", inserted=inserted, updated=updated)
	return result


def get_sync_context(row_name: str) -> SAPSyncContext:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError(
			"The selected row does not belong to an External SAP Configuration."
		)

	mapping = frappe.get_doc("SAP Doctype Mapping", row.sap_mapping)
	if not mapping.enabled:
		raise frappe.ValidationError(f"SAP mapping {mapping.name} is disabled.")
	configuration = frappe.get_doc("External SAP Configuration", row.parent)
	abbr_doctypes = frozenset(
		frappe.get_all(
			"External Site Configuration CT",
			filters={
				"parent": configuration.name,
				"parenttype": "External SAP Configuration",
				"parentfield": "mappings",
				"abbr_required": 1,
			},
			pluck="ref_doctype",
		)
	)
	company_abbr = ""
	if abbr_doctypes:
		company_abbr = frappe.db.get_value("Company", configuration.company, "abbr") or ""
		if not company_abbr:
			frappe.throw(_("Company {0} has no abbreviation.").format(configuration.company))

	bulk_context = BulkInsertContext(
		configuration=configuration.name,
		source_doctype=mapping.external_doctype,
		target_doctype=mapping.erp_doctype,
		company_abbr=company_abbr,
		abbr_doctypes=abbr_doctypes,
		requested_by=frappe.session.user,
	)
	return SAPSyncContext(
		row_name=row.name,
		configuration=configuration.name,
		mapping_name=mapping.name,
		source_doctype=mapping.external_doctype,
		target_doctype=mapping.erp_doctype,
		erp_sap_id_fieldname=mapping.erp_sap_id_fieldname,
		bulk=bulk_context,
	)


def _get_pending_rows(context: SAPSyncContext, started_at) -> list:
	source = frappe.qb.DocType(context.source_doctype)
	return (
		frappe.qb.from_(source)
		.select(source.name, source.remote_id, source.json, source.modified)
		.where(_pending_condition(source, context, started_at))
		.orderby(source.creation, source.name)
		.limit(PAGE_SIZE)
	).run(as_dict=True)


def _count_pending_rows(context: SAPSyncContext, started_at) -> int:
	source = frappe.qb.DocType(context.source_doctype)
	result = (
		frappe.qb.from_(source)
		.select(Count(source.name))
		.where(_pending_condition(source, context, started_at))
	).run()
	return result[0][0] if result else 0


def _pending_condition(source, context: SAPSyncContext, started_at):
	return (
		(source.external_sap_configuration == context.configuration)
		& (source.json != "")
		& (source.modified <= started_at)
		& (
			source.last_synced_on.isnull()
			| (source.last_synced_on == "")
			| (source.modified > source.last_synced_on)
		)
	)


def _prepare_source_row(
	context: SAPSyncContext,
	source_row,
	target_name: str | None = None,
) -> dict[str, list[dict]]:
	try:
		raw = json.loads(source_row.json) if isinstance(source_row.json, str) else source_row.json
	except (TypeError, ValueError):
		frappe.throw(_("Invalid JSON in {0}.").format(source_row.name))
	if not isinstance(raw, dict):
		frappe.throw(_("JSON in {0} must contain a document object.").format(source_row.name))

	raw = dict(raw)
	raw.setdefault("name", target_name or str(source_row.remote_id or source_row.name))
	if context.erp_sap_id_fieldname:
		raw[context.erp_sap_id_fieldname] = str(source_row.remote_id)
	return _prepare_document_tree(context.bulk, raw)


def _get_target_names_by_remote_id(context: SAPSyncContext, source_rows: list) -> dict[str, str]:
	if not context.erp_sap_id_fieldname:
		return {}
	remote_ids = [str(row.remote_id) for row in source_rows]
	rows = frappe.get_all(
		context.target_doctype,
		filters={context.erp_sap_id_fieldname: ["in", remote_ids]},
		fields=["name", context.erp_sap_id_fieldname],
		limit_page_length=0,
	)
	return {
		str(row.get(context.erp_sap_id_fieldname)): row.name
		for row in rows
		if row.get(context.erp_sap_id_fieldname) not in (None, "")
	}


def _mark_rows_synced(source_doctype: str, source_rows: list) -> None:
	names = [row.name for row in source_rows]
	if not names:
		return
	source = frappe.qb.DocType(source_doctype)
	(
		frappe.qb.update(source)
		.set(source.last_synced_on, source.modified)
		.where(source.name.isin(names))
	).run()


def _validate_source(context: SAPSyncContext) -> None:
	for doctype in (context.source_doctype, context.target_doctype):
		if not frappe.db.exists("DocType", doctype):
			frappe.throw(_("DocType {0} is not available.").format(doctype))

	columns = set(frappe.db.get_table_columns(context.source_doctype))
	required = {"remote_id", "external_sap_configuration", "last_synced_on", "json"}
	missing = required - columns
	if missing:
		frappe.throw(
			_("{0} is missing database columns: {1}. Run bench migrate.").format(
				context.source_doctype,
				", ".join(sorted(missing)),
			)
		)
	if context.erp_sap_id_fieldname and not frappe.get_meta(context.target_doctype).has_field(
		context.erp_sap_id_fieldname
	):
		frappe.throw(
			_("{0} has no SAP ID field {1}.").format(
				context.target_doctype,
				context.erp_sap_id_fieldname,
			)
		)


def _log(context: SAPSyncContext, status: str, **details) -> None:
	lines = [
		f"Status: {status}",
		f"Configuration: {context.configuration}",
		f"Mapping: {context.mapping_name}",
		f"Staging DocType: {context.source_doctype}",
		f"ERPNext DocType: {context.target_doctype}",
	]
	lines.extend(f"{key.replace('_', ' ').title()}: {value}" for key, value in details.items())
	frappe.log_error(
		title=f"SAP Sync {status}: {context.mapping_name}"[:140],
		message="\n".join(lines),
		reference_doctype="External SAP Configuration",
		reference_name=context.configuration,
		defer_insert=True,
	)
