from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import getdate, now_datetime

from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.services.fetch import fetch
from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.services.references import SAPReferenceResolver
from emkan_insights.sap.services.staging import StagingWriter


def get_mapped_data(
	mapping_name: str,
	from_date: str | None = None,
	to_date: str | None = None,
	configuration_name: str | None = None,
	row_name: str | None = None,
) -> dict[str, Any]:
	mapping_doc = frappe.get_doc("SAP Doctype Mapping", mapping_name)
	if not mapping_doc.enabled:
		return {"skipped": True, "reason": "Mapping disabled"}

	configuration = _get_configuration(mapping_name, configuration_name)
	row = _get_mapping_row(configuration.name, mapping_name, row_name)
	window_end = now_datetime()
	automatic_window = not from_date and not to_date
	if automatic_window and row and row.last_sync and not mapping_doc.full_snapshot_sync:
		from_date = getdate(row.last_sync).isoformat()
		to_date = getdate(window_end).isoformat()
	_mark_started(row, window_end)
	client = SAPB1Client.from_configuration(configuration.name)
	records = fetch(client, mapping_doc, from_date, to_date)
	mapper = None if getattr(mapping_doc, "raw_payload", False) else Mapper(
		mapping_doc,
		context={
			"company": configuration.company,
			"configuration_name": configuration.name,
			"currency": frappe.db.get_value("Company", configuration.company, "default_currency"),
			"sap_references": SAPReferenceResolver(client),
		},
	)
	writer = StagingWriter(
		mapping_doc.external_doctype,
		configuration.name,
	)
	result = {"success": [], "created": [], "updated": [], "unchanged": [], "failed": []}

	for index, sap_record in enumerate(records):
		_process_record(mapping_doc, mapper, writer, sap_record, index, result)

	_mark_completed(row, window_end, result, automatic_window)
	return result


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
