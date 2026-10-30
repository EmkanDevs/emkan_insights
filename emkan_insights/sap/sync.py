from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import money_in_words, now_datetime
from frappe.utils.nestedset import rebuild_tree

from emkan_insights.emkan_insights.external_data_bulk_insert import (
	PAGE_SIZE,
	BulkInsertContext,
	_insert_document_trees,
	_partition_document_trees,
	_prepare_document_tree,
	_update_document_trees,
	add_prefix,
)
from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.services.mapper import parse_mapping
from emkan_insights.sap.services.staging import SYNC_MAPPING_ERRORS_KEY
from emkan_insights.emkan_insights.import_field_rules import parse_field_rules
from emkan_insights.sap.link_validation import resolve_account_links


PERSISTENT_LOG_STATUSES = {
	"Started", "DocType Creation Completed", "Failed",
	"Skipped Duplicate Item Group", "Skipped Duplicate Unique Value",
}


@dataclass(frozen=True)
class SAPSyncContext:
	row_name: str
	configuration: str
	mapping_name: str
	source_doctype: str
	target_doctype: str
	erp_sap_id_fieldname: str | None
	sap_id_prefix: str
	bulk: BulkInsertContext
	excluded_prefixes: tuple[str, ...] = ()


def sync_mapping(row_name: str, *, remote_ids: set[str] | None = None) -> dict:
	"""Bulk insert pending SAP staging rows into one ERPNext DocType."""
	context = get_sync_context(row_name)
	started_at = now_datetime()
	_validate_source(context)
	_recover_missing_target(context)
	if context.target_doctype == "Journal Entry":
		_recover_incomplete_journals(context)
	inserted = 0
	updated = 0
	skipped = 0

	_log(context, "Started", pending=_count_pending_rows(context, started_at, remote_ids))
	while True:
		source_rows = _get_pending_rows(context, started_at, remote_ids)
		if not source_rows:
			break

		target_names = _get_target_names_by_remote_id(context, source_rows)
		documents = [
			_prepare_source_row(context, row, target_names.get(str(row.remote_id)))
			for row in source_rows
		]
		accepted_rows, documents, unique_skipped = _skip_conflicting_unique_values(
			context, source_rows, documents
		)
		documents, group_skipped = _skip_duplicate_item_groups(context, accepted_rows, documents)
		batch_skipped = unique_skipped + group_skipped
		skipped += batch_skipped
		if documents:
			try:
				resolve_account_links(documents)
				new_documents, existing_documents = _partition_document_trees(
					context.target_doctype,
					documents,
				)
				updated += _update_document_trees(context.target_doctype, existing_documents)
				inserted += _insert_document_trees(new_documents)
			except Exception:
				traceback = frappe.get_traceback(with_context=False)
				frappe.db.rollback()
				_log(context, "Failed", remote_ids=[str(row.remote_id) for row in source_rows], traceback=traceback)
				raise
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
		"skipped": skipped,
	}
	_log(context, "DocType Creation Completed", inserted=inserted, updated=updated, skipped=skipped)
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
	if mapping.fetch_only:
		raise frappe.ValidationError(f"SAP mapping {mapping.name} is fetch only.")
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
		field_rules={
			**{
				fieldname: {"default": rule["default"]}
				for fieldname, rule in parse_mapping(mapping.field_mapping).items()
				if isinstance(rule, dict) and "default" in rule
			},
			**parse_field_rules(getattr(row, "field_rules", None), mapping.erp_doctype),
		},
	)
	sap_id_prefix = getattr(mapping, "sap_id_prefix", None) or ""
	excluded_prefixes = tuple(
		row.sap_id_prefix for row in frappe.get_all(
			"SAP Doctype Mapping",
			filters={"external_doctype": mapping.external_doctype, "enabled": 1},
			fields=["name", "sap_id_prefix"],
		)
		if row.name != mapping.name and row.sap_id_prefix and not sap_id_prefix
	)
	return SAPSyncContext(
		row_name=row.name,
		configuration=configuration.name,
		mapping_name=mapping.name,
		source_doctype=mapping.external_doctype,
		target_doctype=mapping.erp_doctype,
		erp_sap_id_fieldname=mapping.erp_sap_id_fieldname,
		sap_id_prefix=sap_id_prefix,
		bulk=bulk_context,
		excluded_prefixes=excluded_prefixes,
	)


def _get_pending_rows(context: SAPSyncContext, started_at, remote_ids: set[str] | None = None) -> list:
	source = frappe.qb.DocType(context.source_doctype)
	limit = (
		max(PAGE_SIZE, _count_pending_rows(context, started_at, remote_ids))
		if context.target_doctype == "Account" else PAGE_SIZE
	)
	return (
		frappe.qb.from_(source)
		.select(source.name, source.remote_id, source.json, source.modified)
		.where(_pending_condition(source, context, started_at, remote_ids))
		.orderby(source.creation, source.name)
		.limit(limit)
	).run(as_dict=True)


def _count_pending_rows(context: SAPSyncContext, started_at, remote_ids: set[str] | None = None) -> int:
	source = frappe.qb.DocType(context.source_doctype)
	result = (
		frappe.qb.from_(source)
		.select(Count(source.name))
		.where(_pending_condition(source, context, started_at, remote_ids))
	).run()
	return result[0][0] if result else 0


def _pending_condition(source, context: SAPSyncContext, started_at, remote_ids: set[str] | None = None):
	condition = (
		(source.external_sap_configuration == context.configuration)
		& (source.json != "")
		& (source.modified <= started_at)
		& (
			source.last_synced_on.isnull()
			| (source.last_synced_on == "")
			| (source.modified > source.last_synced_on)
		)
	)
	if remote_ids is not None:
		condition &= source.remote_id.isin(sorted(remote_ids)) if remote_ids else source.name == ""
	if context.sap_id_prefix:
		condition &= source.remote_id.like(f"{context.sap_id_prefix}%")
	for prefix in context.excluded_prefixes:
		condition &= ~source.remote_id.like(f"{prefix}%")
	return condition


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
	if errors := raw.get(SYNC_MAPPING_ERRORS_KEY):
		raise frappe.ValidationError(
			f"SAP mapping errors in {source_row.name}: {errors}"
		)
	if "record" in raw and raw.get("sap_mapping"):
		raise frappe.ValidationError("A raw SAP record needs an ERP mapping before sync.")

	raw = dict(raw)
	if context.target_doctype == "Item":
		item_name = raw.get("item_name")
		if isinstance(item_name, str) and len(item_name) > 140:
			raw.setdefault("custom_sap_full_item_name", item_name)
			raw["item_name"] = item_name[:140]
	if context.target_doctype == "Account":
		raw["name"] = target_name or raw.get("account_name") or str(source_row.remote_id or source_row.name)
	else:
		raw.setdefault("name", target_name or str(source_row.remote_id or source_row.name))
	if context.erp_sap_id_fieldname:
		raw[context.erp_sap_id_fieldname] = str(source_row.remote_id)
	if context.target_doctype == "Journal Entry":
		_fill_journal_entry_fields(raw, str(source_row.remote_id))
	Mapper.fill_item_uoms(context.target_doctype, raw)
	document = _prepare_document_tree(context.bulk, raw)
	return document


def _fill_journal_entry_fields(raw: dict, sap_id: str) -> None:
	accounts = raw.get("accounts") or []
	debit = sum((Decimal(str(row.get("debit") or 0)) for row in accounts), Decimal(0))
	credit = sum((Decimal(str(row.get("credit") or 0)) for row in accounts), Decimal(0))
	raw["total_debit"] = debit
	raw["total_credit"] = credit
	raw["difference"] = debit - credit
	raw["custom_sap_jdt_num"] = sap_id
	if accounts:
		raw.setdefault("title", accounts[0].get("account"))
	if raw.get("user_remark"):
		raw.setdefault("remark", f"Note: {raw['user_remark']}")
	raw.setdefault("is_opening", "No")

	debit_accounts = {row.get("party") or row.get("account") for row in accounts if row.get("debit")}
	credit_accounts = {row.get("party") or row.get("account") for row in accounts if row.get("credit")}
	company_currency = frappe.get_cached_value("Company", raw.get("company"), "default_currency")
	raw["multi_currency"] = int(any(
		row.get("account_currency") and row.get("account_currency") != company_currency
		for row in accounts
	))
	for row in accounts:
		if row.get("debit"):
			row["against_account"] = ", ".join(sorted(name for name in credit_accounts if name))
		elif row.get("credit"):
			row["against_account"] = ", ".join(sorted(name for name in debit_accounts if name))
		account = row.get("account")
		if account:
			row["account_type"] = frappe.get_cached_value("Account", account, "account_type")
		account_amount = Decimal(str(row.get("debit_in_account_currency") or row.get("credit_in_account_currency") or 0))
		base_amount = Decimal(str(row.get("debit") or row.get("credit") or 0))
		row["exchange_rate"] = base_amount / account_amount if account_amount else Decimal(1)

	party_row = next(
		(row for row in accounts if row.get("party_type") in {"Customer", "Supplier"} and row.get("party")),
		None,
	)
	if party_row:
		party = party_row["party"]
		currency = party_row.get("account_currency") or company_currency
		amount = sum(
			(Decimal(str(row.get("debit_in_account_currency") or row.get("credit_in_account_currency") or 0))
			 for row in accounts if row.get("party") == party),
			Decimal(0),
		)
		party_name_field = "customer_name" if party_row["party_type"] == "Customer" else "supplier_name"
		raw["pay_to_recd_from"] = frappe.db.get_value(
			party_row["party_type"], party, party_name_field
		) or party
		raw["total_amount"] = amount
		raw["total_amount_currency"] = currency
		raw["total_amount_in_words"] = money_in_words(float(amount), currency)


def _get_target_names_by_remote_id(context: SAPSyncContext, source_rows: list) -> dict[str, str]:
	if not context.erp_sap_id_fieldname:
		if context.target_doctype == "Account":
			legacy_names = {
				str(row.remote_id): (
					add_prefix(str(row.remote_id), context.bulk.company_abbr)
					if "Account" in context.bulk.abbr_doctypes else str(row.remote_id)
				)
				for row in source_rows
			}
			existing = set(frappe.get_all(
				"Account",
				filters={"name": ["in", list(legacy_names.values())]},
				pluck="name",
				limit_page_length=0,
			))
			return {
				remote_id: name for remote_id, name in legacy_names.items() if name in existing
			}
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


def _skip_conflicting_unique_values(
	context: SAPSyncContext, source_rows: list, documents: list
) -> tuple[list, list, int]:
	unique_fields = [field.fieldname for field in frappe.get_meta(context.target_doctype).fields if field.unique]
	if not unique_fields:
		return source_rows, documents, 0

	owners_by_field = {}
	for fieldname in unique_fields:
		values = {
			tree[context.target_doctype][0].get(fieldname)
			for tree in documents
			if tree[context.target_doctype][0].get(fieldname) not in (None, "")
		}
		owners_by_field[fieldname] = {
			_unique_value_key(row.get(fieldname)): row.name
			for row in frappe.get_all(
				context.target_doctype,
				filters={fieldname: ["in", list(values)]},
				fields=["name", fieldname],
				limit_page_length=0,
			)
		} if values else {}

	accepted_rows, accepted_documents = [], []
	for source_row, tree in zip(source_rows, documents, strict=True):
		parent = tree[context.target_doctype][0]
		conflict = next((
			(fieldname, owner)
			for fieldname in unique_fields
			if (value := parent.get(fieldname)) not in (None, "")
			and (owner := owners_by_field[fieldname].get(_unique_value_key(value)))
			and owner != parent["name"]
		), None)
		if conflict:
			fieldname, owner = conflict
			_log(
				context, "Skipped Duplicate Unique Value",
				source_row=source_row.name,
				remote_id=source_row.remote_id,
				field=fieldname,
				value=parent[fieldname],
				existing_document=owner,
			)
			continue
		accepted_rows.append(source_row)
		accepted_documents.append(tree)
		for fieldname in unique_fields:
			if (value := parent.get(fieldname)) not in (None, ""):
				owners_by_field[fieldname][_unique_value_key(value)] = parent["name"]
	return accepted_rows, accepted_documents, len(source_rows) - len(accepted_rows)


def _unique_value_key(value):
	return value.rstrip().casefold() if isinstance(value, str) else value


def _skip_duplicate_item_groups(context: SAPSyncContext, source_rows: list, documents: list) -> tuple[list, int]:
	if context.target_doctype != "Item Group":
		return documents, 0

	group_names = {
		tree["Item Group"][0].get("item_group_name")
		for tree in documents
		if tree["Item Group"][0].get("item_group_name")
	}
	existing = {
		_item_group_name_key(row.item_group_name): row.name
		for row in frappe.get_all(
			"Item Group",
			filters={"item_group_name": ["in", list(group_names)]},
			fields=["name", "item_group_name"],
			limit_page_length=0,
		)
	} if group_names else {}

	accepted = []
	skipped = 0
	for source_row, tree in zip(source_rows, documents, strict=True):
		item_group = tree["Item Group"][0]
		group_name = item_group.get("item_group_name")
		group_key = _item_group_name_key(group_name) if group_name else None
		owner = existing.get(group_key)
		if owner and owner.casefold() != item_group["name"].casefold():
			skipped += 1
			_log(
				context, "Skipped Duplicate Item Group",
				source_row=source_row.name,
				item_group_name=group_name,
				existing_item_group=owner,
			)
			continue
		accepted.append(tree)
		if group_key:
			existing[group_key] = item_group["name"]
	return accepted, skipped


def _item_group_name_key(value: str) -> str:
	return value.rstrip().casefold()


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


def _recover_missing_target(context: SAPSyncContext) -> None:
	if context.target_doctype == "Account":
		company = frappe.db.get_value("External SAP Configuration", context.configuration, "company")
		missing = not frappe.db.count("Account", {"company": company, "account_number": ["is", "set"]})
	else:
		missing = not frappe.db.count(context.target_doctype)
	if not missing:
		return
	source = frappe.qb.DocType(context.source_doctype)
	(
		frappe.qb.update(source)
		.set(source.last_synced_on, None)
		.where(source.external_sap_configuration == context.configuration)
	).run()
	frappe.db.commit()


def _recover_incomplete_journals(context: SAPSyncContext) -> None:
	prefix = (
		f"{context.bulk.company_abbr}-"
		if "Journal Entry" in context.bulk.abbr_doctypes else ""
	)
	broken = frappe.db.sql(
		"""
		SELECT DISTINCT staged.name
		FROM `tabJournal Entry` journal
		JOIN `tabExternal Journal Entry` staged
			ON journal.name = CONCAT(%s, staged.remote_id)
		JOIN `tabJournal Entry Account` account ON account.parent = journal.name
		WHERE staged.external_sap_configuration = %s
			AND (
				(journal.total_debit = 0 AND (account.debit != 0 OR account.credit != 0))
				OR (journal.total_amount = 0 AND account.party_type IN ('Customer', 'Supplier')
					AND account.party IS NOT NULL)
			)
		""",
		(prefix, context.configuration),
	)
	if not broken:
		return
	source = frappe.qb.DocType(context.source_doctype)
	(
		frappe.qb.update(source)
		.set(source.last_synced_on, None)
		.where(source.name.isin([name for name, in broken]))
	).run()
	frappe.db.commit()


def _log(context: SAPSyncContext, status: str, **details) -> None:
	lines = [
		f"Status: {status}",
		f"Configuration: {context.configuration}",
		f"Mapping: {context.mapping_name}",
		f"Staging DocType: {context.source_doctype}",
		f"ERPNext DocType: {context.target_doctype}",
	]
	lines.extend(f"{key.replace('_', ' ').title()}: {value}" for key, value in details.items())
	title = (
		f"SAP DocType Creation Completed: {context.target_doctype}"
		if status == "DocType Creation Completed"
		else f"SAP Sync {status}: {context.mapping_name}"
	)
	frappe.log_error(
		title=title[:140],
		message="\n".join(lines),
		reference_doctype="External SAP Configuration",
		reference_name=context.configuration,
		defer_insert=status not in PERSISTENT_LOG_STATUSES,
	)
	if status in PERSISTENT_LOG_STATUSES:
		frappe.db.commit()
