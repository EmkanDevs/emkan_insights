import json
from dataclasses import dataclass
from functools import lru_cache

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import cint, now_datetime
from frappe.utils.nestedset import rebuild_tree

from emkan_insights.emkan_insights.external_data_fetch import is_deferred_naming_pending


PAGE_SIZE = 500
BULK_INSERT_LOCK_TTL = 7200
ACCOUNT_EXCLUDED_FIELDS = {"lft", "rgt", "old_parent"}


@dataclass(frozen=True)
class BulkInsertContext:
	configuration: str
	source_doctype: str
	target_doctype: str
	company_abbr: str
	abbr_doctypes: frozenset[str]
	requested_by: str
	source_site_url: str = ""


@frappe.whitelist(methods=["POST"])
def queue_external_data_bulk_insert(configuration_name: str) -> dict:
	configuration = frappe.get_doc("External Site Configuration", configuration_name)
	frappe.has_permission(
		"External Site Configuration",
		"write",
		doc=configuration.name,
		throw=True,
	)
	rows = _ordered_configuration_rows(configuration)
	if not rows:
		frappe.throw(_("No DocTypes are configured for bulk insert."))

	lock_key = _lock_key(configuration.name)
	if not frappe.cache().set(lock_key, "1", nx=True, ex=BULK_INSERT_LOCK_TTL):
		frappe.throw(_("Another sync or bulk insert is already running for this configuration."))

	try:
		frappe.enqueue(
			"emkan_insights.emkan_insights.external_data_bulk_insert.bulk_insert_external_data",
			queue="long",
			timeout=7200,
			enqueue_after_commit=True,
			job_name=f"external_data_bulk_insert_{configuration.name}",
			configuration_name=configuration.name,
			requested_by=frappe.session.user,
		)
	except Exception:
		frappe.cache().delete(lock_key)
		raise

	return {
		"status": "queued",
		"message": _("Bulk insert queued for {0} DocTypes.").format(len(rows)),
	}


def bulk_insert_external_data(
	configuration_name: str,
	requested_by: str | None = None,
	*,
	release_lock: bool = True,
) -> dict:
	try:
		configuration = frappe.get_doc("External Site Configuration", configuration_name)
		requested_by = requested_by or frappe.session.user
		bulk_insert_started_at = now_datetime()
		results = []
		for row in _ordered_configuration_rows(configuration):
			context = _get_context(row.name, requested_by=requested_by)
			results.append(_bulk_insert_records(context, bulk_insert_started_at))

		result = _summarize_results(configuration.name, bulk_insert_started_at, results)
		frappe.logger("external_data_bulk_insert").info("External data bulk insert complete: %s", result)
		frappe.publish_realtime(
			"external_data_bulk_insert_complete",
			result,
			user=requested_by,
			after_commit=True,
		)
	finally:
		if release_lock:
			frappe.cache().delete(_lock_key(configuration_name))

	if release_lock:
		from emkan_insights.emkan_insights.external_data_deletion import (
			enqueue_external_data_deletion_reconciliation,
		)

		result["deletion_reconciliation"] = enqueue_external_data_deletion_reconciliation(
			configuration_name,
		)
	return result


def _ordered_configuration_rows(configuration) -> list:
	return sorted(
		configuration.fetch_data or [],
		key=lambda row: (row.idx or 0, row.ref_doctype or ""),
	)


def _summarize_results(configuration_name: str, started_at, results: list[dict]) -> dict:
	return {
		"status": "complete",
		"configuration": configuration_name,
		"started_at": started_at,
		"processed_doctypes": len(results),
		"inserted": sum(result["inserted"] for result in results),
		"updated": sum(result["updated"] for result in results),
		"results": results,
	}


def transform_row(
	doctype: str,
	row: dict,
	prefix: str,
	abbr_doctypes: set[str] | frozenset[str],
	*,
	prefix_name: bool | None = None,
	link_fields: dict | None = None,
) -> dict:
	result = dict(row)
	link_fields = link_fields if link_fields is not None else get_link_fields(doctype)
	should_prefix_name = doctype in abbr_doctypes if prefix_name is None else prefix_name

	if should_prefix_name:
		result["name"] = add_prefix(result.get("name"), prefix)

	if row.get("parenttype") in abbr_doctypes:
		result["parent"] = add_prefix(result.get("parent"), prefix)

	for fieldname, linked in link_fields.items():
		value = result.get(fieldname)
		if not value:
			continue

		linked_doctype = row.get(linked["options_field"]) if isinstance(linked, dict) else linked
		if linked_doctype in abbr_doctypes:
			result[fieldname] = add_prefix(value, prefix)

	return result


def add_prefix(value, prefix: str):
	if value in (None, "") or not prefix:
		return value

	value = str(value)
	return value if value.startswith(f"{prefix}-") else f"{prefix}-{value}"


@lru_cache(maxsize=128)
def get_link_fields(doctype: str) -> dict:
	link_fields = {}
	for field in frappe.get_meta(doctype).fields:
		if field.fieldtype == "Link":
			link_fields[field.fieldname] = field.options
		elif field.fieldtype == "Dynamic Link":
			link_fields[field.fieldname] = {"options_field": field.options}
	return link_fields


def _get_context(child_docname: str, requested_by: str | None = None) -> BulkInsertContext:
	row = frappe.db.get_value(
		"External Site Configuration CT",
		child_docname,
		["parent", "ref_doctype", "exported_doctype"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("External Site Configuration row {0} was not found.").format(child_docname))

	for doctype in (row.ref_doctype, row.exported_doctype):
		if not doctype or not frappe.db.exists("DocType", doctype):
			frappe.throw(_("DocType {0} is not available.").format(doctype or ""))

	configuration = frappe.get_doc("External Site Configuration", row.parent)
	abbr_doctypes = frozenset(
		frappe.get_all(
			"External Site Configuration CT",
			filters={"parent": configuration.name, "abbr_required": 1},
			pluck="ref_doctype",
		)
	)
	company_abbr = ""
	if abbr_doctypes:
		company_abbr = frappe.db.get_value("Company", configuration.company, "abbr") or ""
		if not company_abbr:
			frappe.throw(_("Company {0} has no abbreviation.").format(configuration.company))

	return BulkInsertContext(
		configuration=configuration.name,
		source_doctype=row.exported_doctype,
		target_doctype=row.ref_doctype,
		company_abbr=company_abbr,
		abbr_doctypes=abbr_doctypes,
		requested_by=requested_by or frappe.session.user,
		source_site_url=configuration.site_url or "",
	)


def _bulk_insert_records(context: BulkInsertContext, started_at) -> dict:
	_validate_source(context.source_doctype)
	inserted = 0
	updated = 0
	frappe.log_error(
		message=_("Starting bulk insert for {0}. Records: {1}").format(
			context.target_doctype,
			_count_pending_source_rows(context, started_at),
		),
		title=f"External Data Bulk Insert: {context.target_doctype}",
	)
	while True:
		source_rows = _get_pending_source_rows(context, started_at)
		if not source_rows:
			break

		prepared = []
		for source_row in source_rows:
			raw = _parse_json(source_row.json, source_row.name)
			if is_deferred_naming_pending(context.target_doctype, raw):
				continue
			raw.setdefault("name", source_row.remote_id or source_row.name)
			prepared.append(_prepare_document_tree(context, raw))

		if prepared:
			new_documents, existing_documents = _partition_document_trees(
				context.target_doctype,
				prepared,
			)
			inserted += _insert_document_trees(new_documents)
			updated += _update_document_trees(context.target_doctype, existing_documents)
		_mark_source_rows_synced(context.source_doctype, source_rows)
		frappe.db.commit()
	if context.target_doctype == "Account":
		rebuild_tree("Account", "parent_account")
	frappe.log_error(
		message=_("Bulk insert complete for {0}. Inserted: {1}, Updated: {2}").format(
			context.target_doctype, inserted, updated
		),
		title=f"External Data Bulk Insert: {context.target_doctype}",
	)
	return {
		"status": "complete",
		"source_doctype": context.source_doctype,
		"target_doctype": context.target_doctype,
		"inserted": inserted,
		"updated": updated,
	}


def _get_pending_source_rows(context: BulkInsertContext, started_at) -> list:
	source = frappe.qb.DocType(context.source_doctype)
	return (
		frappe.qb.from_(source)
		.select(source.name, source.remote_id, source.json, source.modified)
		.where(_pending_source_condition(source, context, started_at))
		.orderby(source.creation, source.name)
		.limit(PAGE_SIZE)
	).run(as_dict=True)


def _count_pending_source_rows(context: BulkInsertContext, started_at) -> int:
	source = frappe.qb.DocType(context.source_doctype)
	result = (
		frappe.qb.from_(source)
		.select(Count(source.name))
		.where(_pending_source_condition(source, context, started_at))
	).run()
	return result[0][0] if result else 0


def _pending_source_condition(source, context: BulkInsertContext, started_at):
	return (
		_source_site_condition(source, context)
		& (source.json != "")
		& (source.modified <= started_at)
		& (
			source.last_synced_on.isnull()
			| (source.last_synced_on == "")
			| (source.modified > source.last_synced_on)
		)
	)


def _source_site_condition(source, context: BulkInsertContext):
	columns = set(frappe.db.get_table_columns(context.source_doctype))
	condition = None
	if "source_site" in columns:
		condition = source.source_site == context.configuration

	if "site_url" in columns and context.source_site_url:
		clean_url = context.source_site_url.rstrip("/")
		url_condition = source.site_url.isin((clean_url, f"{clean_url}/"))
		condition = condition | url_condition if condition is not None else url_condition

	if condition is None:
		frappe.throw(
			_("{0} must have a source_site or site_url column. Run bench migrate.").format(
				context.source_doctype
			)
		)
	return condition


def _mark_source_rows_synced(source_doctype: str, source_rows: list) -> None:
	names = [row.name for row in source_rows]
	if not names:
		return

	source = frappe.qb.DocType(source_doctype)
	(
		frappe.qb.update(source)
		.set(source.last_synced_on, source.modified)
		.where(source.name.isin(names))
	).run()


def _prepare_document_tree(context: BulkInsertContext, raw: dict) -> dict[str, list[dict]]:
	now = now_datetime()
	parent = _prepare_standard_fields(raw, context.requested_by, now)
	parent = transform_row(
		context.target_doctype,
		parent,
		context.company_abbr,
		context.abbr_doctypes,
	)
	rows_by_doctype = {context.target_doctype: [parent]}

	for table_field in frappe.get_meta(context.target_doctype).get_table_fields():
		child_rows = raw.get(table_field.fieldname) or []
		for index, child in enumerate(child_rows, start=1):
			if not isinstance(child, dict):
				continue
			child = _prepare_standard_fields(child, context.requested_by, now)
			child.setdefault("name", frappe.generate_hash(length=10))
			child.update({
				"parent": parent["name"],
				"parenttype": context.target_doctype,
				"parentfield": table_field.fieldname,
				"idx": child.get("idx") or index,
			})
			child = transform_row(
				table_field.options,
				child,
				context.company_abbr,
				context.abbr_doctypes,
				prefix_name=context.target_doctype in context.abbr_doctypes,
			)
			rows_by_doctype.setdefault(table_field.options, []).append(child)

	return rows_by_doctype


def _prepare_standard_fields(row: dict, user: str, fallback_time) -> dict:
	result = dict(row)
	result["creation"] = row.get("creation") or fallback_time
	result["modified"] = row.get("modified") or result["creation"]
	result["owner"] = row.get("owner") or user
	result["modified_by"] = row.get("modified_by") or user
	result["docstatus"] = cint(row.get("docstatus", 0))
	return result


def _partition_document_trees(doctype: str, documents: list[dict]) -> tuple[list[dict], list[dict]]:
	names = [tree[doctype][0]["name"] for tree in documents]
	existing = set(
		frappe.get_all(doctype, filters={"name": ["in", names]}, pluck="name", limit_page_length=0)
	)
	new_documents = [tree for tree in documents if tree[doctype][0]["name"] not in existing]
	existing_documents = [tree for tree in documents if tree[doctype][0]["name"] in existing]
	return new_documents, existing_documents


def _update_document_trees(parent_doctype: str, documents: list[dict]) -> int:
	if not documents:
		return 0

	parent_columns = set(_get_import_columns(parent_doctype))
	updates = {}
	parent_names = []
	for tree in documents:
		parent = tree[parent_doctype][0]
		parent_names.append(parent["name"])
		updates[parent["name"]] = {
			fieldname: value
			for fieldname, value in parent.items()
			if fieldname != "name" and fieldname in parent_columns
		}

	frappe.db.bulk_update(
		parent_doctype,
		updates,
		chunk_size=PAGE_SIZE,
		update_modified=False,
	)
	_clear_child_rows(parent_doctype, parent_names)
	_insert_document_trees([
		{doctype: rows for doctype, rows in tree.items() if doctype != parent_doctype}
		for tree in documents
	])
	return len(documents)


def _clear_child_rows(parent_doctype: str, parent_names: list[str]) -> None:
	for table_field in frappe.get_meta(parent_doctype).get_table_fields():
		frappe.db.delete(
			table_field.options,
			{"parent": ["in", parent_names], "parenttype": parent_doctype},
		)


def _insert_document_trees(documents: list[dict]) -> int:
	rows_by_doctype = {}
	for tree in documents:
		for doctype, rows in tree.items():
			rows_by_doctype.setdefault(doctype, []).extend(rows)

	for doctype, rows in rows_by_doctype.items():
		insert_columns = _get_import_columns(doctype)
		values = [tuple(row.get(fieldname) for fieldname in insert_columns) for row in rows]
		frappe.db.bulk_insert(
			doctype,
			fields=insert_columns,
			values=values,
			ignore_duplicates=True,
			chunk_size=PAGE_SIZE,
		)

	return len(documents)


def _get_import_columns(doctype: str) -> list[str]:
	columns = frappe.db.get_table_columns(doctype)
	if doctype == "Account":
		return [fieldname for fieldname in columns if fieldname not in ACCOUNT_EXCLUDED_FIELDS]
	return columns


def _parse_json(value, source_name: str) -> dict:
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError):
		frappe.throw(_("Invalid JSON in {0}.").format(source_name))
	if not isinstance(parsed, dict):
		frappe.throw(_("JSON in {0} must contain a document object.").format(source_name))
	return parsed


def _validate_source(source_doctype: str) -> None:
	meta = frappe.get_meta(source_doctype)
	columns = set(frappe.db.get_table_columns(source_doctype))
	for fieldname in ("remote_id", "json", "last_synced_on"):
		if not meta.has_field(fieldname):
			frappe.throw(_("{0} must have a {1} field.").format(source_doctype, fieldname))
		if fieldname not in columns:
			frappe.throw(
				_("{0} is missing the {1} database column. Run bench migrate.").format(
					source_doctype,
					fieldname,
				)
			)

	if not columns.intersection({"source_site", "site_url"}):
		frappe.throw(
			_("{0} must have a source_site or site_url column. Run bench migrate.").format(
				source_doctype
			)
		)


def _lock_key(configuration_name: str) -> str:
	return f"external_site_pipeline_lock:{configuration_name}"
