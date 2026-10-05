from __future__ import annotations

import frappe
from frappe.utils import getdate

from emkan_insights.sap.services.client import SAPB1Client


def fetch(
	client: SAPB1Client,
	mapping_doc,
	from_date: str | None = None,
	to_date: str | None = None,
) -> list[dict]:
	params = _query_params(mapping_doc, from_date, to_date)
	records = client.fetch_all(mapping_doc.sap_doctype, params=params)
	return _flatten_records(records, mapping_doc.source_array_field)


def _query_params(mapping_doc, from_date: str | None, to_date: str | None) -> dict[str, str] | None:
	filters = []
	if mapping_doc.sap_filter:
		filters.append(f"({mapping_doc.sap_filter.strip()})")

	date_params = _date_filter(mapping_doc.date_filter_field, from_date, to_date)
	if date_params:
		filters.append(f"({date_params['$filter']})")

	return {"$filter": " and ".join(filters)} if filters else None


def _flatten_records(records: list[dict], source_array_field: str | None) -> list[dict]:
	if not source_array_field:
		return records

	flattened = []
	for record in records:
		rows = record.get(source_array_field) or []
		if not isinstance(rows, list):
			raise frappe.ValidationError(f"SAP field '{source_array_field}' must contain an array.")
		for row in rows:
			if not isinstance(row, dict):
				raise frappe.ValidationError(f"SAP field '{source_array_field}' must contain JSON objects.")
			flattened.append(row)
	return flattened


def _date_filter(
	fieldname: str | None, from_date: str | None, to_date: str | None
) -> dict[str, str] | None:
	if not fieldname:
		return None
	if not from_date and not to_date:
		return None
	if not from_date or not to_date:
		raise frappe.ValidationError("From Date and To Date are required for this mapping.")

	start = getdate(from_date)
	end = getdate(to_date)
	if start > end:
		raise frappe.ValidationError("From Date cannot be after To Date.")

	return {"$filter": f"{fieldname} ge '{start.isoformat()}' and {fieldname} le '{end.isoformat()}'"}
