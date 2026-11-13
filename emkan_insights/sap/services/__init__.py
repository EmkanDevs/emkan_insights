from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, getdate, now_datetime

from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.services.fetch import fetch
from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.services.references import SAPReferenceResolver
from emkan_insights.sap.services.staging import StagingWriter


STAGING_COMMIT_BATCH_SIZE = 500


def get_mapped_data(
	mapping_name: str,
	from_date: str | None = None,
	to_date: str | None = None,
	configuration_name: str | None = None,
	row_name: str | None = None,
	only_ids: list[int | str] | None = None,
) -> dict[str, Any]:
	"""Fetch, map and stage SAP records.

	only_ids refetches just those SAP keys. It leaves last_sync and the fetch cursor
	alone, so it can repair rows that failed mapping without a snapshot refetch.
	"""
	mapping_doc = frappe.get_doc("SAP Doctype Mapping", mapping_name)
	if not mapping_doc.enabled:
		return {"skipped": True, "reason": "Mapping disabled"}

	configuration = _get_configuration(mapping_name, configuration_name)
	row = _get_mapping_row(configuration.name, mapping_name, row_name)
	window_end = now_datetime()
	automatic_window = not from_date and not to_date and only_ids is None
	uses_cursor = (
		bool(getattr(mapping_doc, "cursor_field", None)) and not mapping_doc.full_snapshot_sync
		and only_ids is None
	)
	cursor_after = None
	if uses_cursor:
		# An explicit date window is a request for a full refresh, so it skips the cursor.
		cursor_after = _staged_cursor(mapping_doc, configuration.name) if automatic_window else None
	elif automatic_window and row and row.last_sync and not mapping_doc.full_snapshot_sync:
		from_date = getdate(row.last_sync).isoformat()
		to_date = getdate(window_end).isoformat()
	if only_ids is None:
		_mark_started(row, window_end)
	client = SAPB1Client.from_configuration(configuration.name)
	if only_ids is not None:
		records = fetch(client, mapping_doc, only_ids=only_ids)
	elif uses_cursor:
		records = fetch(client, mapping_doc, from_date, to_date, cursor_after=cursor_after)
	else:
		records = fetch(client, mapping_doc, from_date, to_date)
	account_parents = (
		{str(record["Code"]): record.get("FatherAccountKey") for record in records if record.get("Code")}
		if mapping_doc.name == "SAP Account" else {}
	)
	account_names = (
		{str(record["Code"]): record.get("Name") for record in records if record.get("Code")}
		if mapping_doc.name == "SAP Account" else {}
	)
	account_party_types = _account_party_types(client) if mapping_doc.name == "SAP Account" else {}
	mapper = None if getattr(mapping_doc, "raw_payload", False) else Mapper(
		mapping_doc,
		context={
			"company": configuration.company,
			"configuration_name": configuration.name,
			"company_abbr": frappe.get_cached_value("Company", configuration.company, "abbr"),
			"prefixed_doctypes": frozenset(frappe.get_all(
				"External Site Configuration CT",
				filters={"parent": configuration.name, "abbr_required": 1},
				pluck="ref_doctype",
			)),
			"currency": frappe.db.get_value("Company", configuration.company, "default_currency"),
			"sap_references": SAPReferenceResolver(client),
			"account_parents": account_parents,
			"account_names": account_names,
			"account_party_types": account_party_types,
		},
	)
	_prime_references(mapping_doc, mapper, records)
	writer = StagingWriter(
		mapping_doc.external_doctype,
		configuration.name,
	)
	result = {"success": [], "created": [], "updated": [], "unchanged": [], "failed": []}

	for index, sap_record in enumerate(records):
		if index % STAGING_COMMIT_BATCH_SIZE == 0:
			writer.prefetch(_batch_sap_ids(
				mapping_doc, records[index:index + STAGING_COMMIT_BATCH_SIZE]
			))
		_process_record(mapping_doc, mapper, writer, sap_record, index, result)
		if (index + 1) % STAGING_COMMIT_BATCH_SIZE == 0:
			writer.flush()
			frappe.db.commit()
	writer.flush()

	if only_ids is None:
		_mark_completed(row, window_end, result, automatic_window)
	return result


def _staged_cursor(mapping_doc, configuration_name: str) -> int | None:
	"""Key to fetch after: highest staged numeric key minus the overlap.

	Staged rows that failed mapping pull the cursor back to the oldest failed key, so
	they are fetched again until they map. Returns None when nothing is staged yet.
	"""
	if not frappe.db.table_exists(mapping_doc.external_doctype):
		return None
	prefix = getattr(mapping_doc, "sap_id_prefix", None) or ""
	key = "CAST(SUBSTRING_INDEX(SUBSTRING(remote_id, %(skip)s), ':', 1) AS UNSIGNED)"
	where = (
		"external_sap_configuration = %(config)s AND remote_id LIKE %(like)s "
		"AND SUBSTRING_INDEX(SUBSTRING(remote_id, %(skip)s), ':', 1) REGEXP '^[0-9]+$'"
	)
	values = {
		"config": configuration_name, "skip": len(prefix) + 1,
		"like": prefix.replace("%", "\\%").replace("_", "\\_") + "%", "errors": "%_sap_mapping_errors%",
	}
	table = f"`tab{mapping_doc.external_doctype}`"
	highest = frappe.db.sql(f"SELECT MAX({key}) FROM {table} WHERE {where}", values)[0][0]
	if highest is None:
		return None
	oldest_failed = frappe.db.sql(
		f"SELECT MIN({key}) FROM {table} WHERE {where} AND `json` LIKE %(errors)s", values
	)[0][0]
	cursor = int(highest) - cint(getattr(mapping_doc, "cursor_overlap", 0))
	if oldest_failed is not None:
		cursor = min(cursor, int(oldest_failed) - 1)
	return max(cursor, 0)


def _prime_references(mapping_doc, mapper, records: list[dict]) -> None:
	resolver = mapper.context.get("sap_references") if mapper is not None and hasattr(mapper, "context") else None
	if resolver is None:
		return
	if mapping_doc.name == "SAP Purchase Order":
		resolver.prime_purchase_order_sources(records)
	elif mapping_doc.name == "SAP Purchase Invoice":
		resolver.prime_purchase_invoice_receipts(records)


def _batch_sap_ids(mapping_doc, records: list[dict]) -> list[str]:
	prefix = getattr(mapping_doc, "sap_id_prefix", "") or ""
	ids = (_sap_record_id(record, mapping_doc.sap_id_field) for record in records)
	return [f"{prefix}{raw_id}" for raw_id in ids if raw_id not in (None, "")]


def _account_party_types(client: SAPB1Client) -> dict[str, str]:
	types = {}
	for partner in client.fetch_all("BusinessPartners", {
		"$select": "CardType,DebitorAccount",
	}):
		account = partner.get("DebitorAccount")
		account_type = {"cCustomer": "Receivable", "cSupplier": "Payable"}.get(partner.get("CardType"))
		if not account or not account_type:
			continue
		if account in types and types[account] != account_type:
			raise frappe.ValidationError(
				f"SAP account {account} is used by both customers and suppliers."
			)
		types[account] = account_type
	return types


def _get_mapping_row(configuration_name: str, mapping_name: str, row_name: str | None):
	filters = {
		"parent": configuration_name,
		"parenttype": "External SAP Configuration",
		"parentfield": "mappings",
		"sap_mapping": mapping_name,
	}
	if row_name:
		filters["name"] = row_name
	row = frappe.db.get_value(
		"External Site Configuration CT",
		filters,
		["name", "last_sync"],
		as_dict=True,
	)
	if row_name and not row:
		raise frappe.ValidationError("The SAP mapping row does not belong to this configuration.")
	return row


def _mark_started(row, window_end) -> None:
	if not row:
		return
	frappe.db.set_value(
		"External Site Configuration CT",
		row.name,
		{"fetch_started_at": window_end, "fetch_completed_at": None},
		update_modified=False,
	)
	frappe.db.commit()


def _mark_completed(row, window_end, result: dict[str, Any], automatic_window: bool) -> None:
	if not row:
		return
	values = {"fetch_completed_at": now_datetime()}
	if automatic_window and not result["failed"]:
		values["last_sync"] = window_end
	frappe.db.set_value(
		"External Site Configuration CT",
		row.name,
		values,
		update_modified=False,
	)
	result["last_sync"] = values.get("last_sync")


def _get_configuration(mapping_name: str, configuration_name: str | None):
	if configuration_name:
		return frappe.get_doc("External SAP Configuration", configuration_name)

	configuration_name = frappe.db.get_value(
		"External Site Configuration CT",
		{
			"sap_mapping": mapping_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
		},
		"parent",
	)
	if not configuration_name:
		raise frappe.ValidationError(f"No External SAP Configuration contains mapping '{mapping_name}'.")
	return frappe.get_doc("External SAP Configuration", configuration_name)


def _process_record(
	mapping_doc,
	mapper: Mapper | None,
	writer: StagingWriter,
	sap_record: dict,
	index: int,
	result: dict,
) -> None:
	raw_id = _sap_record_id(sap_record, mapping_doc.sap_id_field)
	if raw_id in (None, ""):
		raise frappe.ValidationError(
			f"SAP record #{index + 1} for {mapping_doc.name} has no {mapping_doc.sap_id_field}."
		)
	sap_id = f"{getattr(mapping_doc, 'sap_id_prefix', '') or ''}{raw_id}"

	savepoint = f"sap_sync_{index}"
	frappe.db.savepoint(savepoint)
	try:
		if getattr(mapping_doc, "raw_payload", False):
			payload = {
				"sap_mapping": mapping_doc.name,
				"source_endpoint": mapping_doc.sap_doctype,
				"record": sap_record,
			}
		else:
			payload = mapper.map(sap_record)
		action, _staging_name = writer.upsert(sap_id, payload)
		result["success"].append(sap_id)
		result[action].append(sap_id)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		result["failed"].append(sap_id)
		frappe.log_error(
			title=f"SAP Fetch Record Failed: {mapping_doc.name} #{sap_id}"[:140],
			message=f"{exc}\n\n{frappe.get_traceback(with_context=False)}",
			defer_insert=True,
		)
		raise


def _sap_record_id(record: dict, id_field: str) -> str | None:
	parts = [record.get(field.strip()) for field in id_field.split("+")]
	if any(part in (None, "") for part in parts):
		return None
	return ":".join(str(part) for part in parts)
