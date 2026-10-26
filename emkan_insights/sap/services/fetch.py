from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import cint, getdate

from emkan_insights.sap.services.client import SAPB1Client


def fetch(
	client: SAPB1Client,
	mapping_doc,
	from_date: str | None = None,
	to_date: str | None = None,
) -> list[dict]:
	params = _query_params(mapping_doc, from_date, to_date)
	limit = cint(getattr(mapping_doc, "fetch_limit", 0))
	if limit:
		params = {**(params or {}), "$top": str(limit)}
		records = client.fetch_all(mapping_doc.sap_doctype, params=params, max_records=limit)
	else:
		records = client.fetch_all(mapping_doc.sap_doctype, params=params)
	return _flatten_records(
		records, mapping_doc.source_array_field,
		include_parent="+" in mapping_doc.sap_id_field,
	)


def _query_params(mapping_doc, from_date: str | None, to_date: str | None) -> dict[str, str] | None:
	filters = []
	if mapping_doc.sap_filter:
		filters.append(f"({mapping_doc.sap_filter.strip()})")

	date_params = None
	if not getattr(mapping_doc, "full_snapshot_sync", False):
		creation_field = getattr(mapping_doc, "creation_date_filter_field", None)
		update_field = mapping_doc.date_filter_field
		if not (creation_field or update_field):
			raise frappe.ValidationError(
				"Incremental sync requires a SAP creation or update date field."
			)
		date_params = _date_filter(
			creation_field,
			update_field,
			from_date,
			to_date,
		)
	if date_params:
		filters.append(f"({date_params['$filter']})")

	return {"$filter": " and ".join(filters)} if filters else None


def _flatten_records(
	records: list[dict], source_array_field: str | None, include_parent: bool = False
) -> list[dict]:
	if not source_array_field:
		return records

	flattened = []
	for record in records:
		rows = record.get(source_array_field) or []
		if not isinstance(rows, list):
			raise frappe.ValidationError(f"SAP field '{source_array_field}' must contain an array.")
		parent_fields = {key: value for key, value in record.items() if key != source_array_field}
		for index, row in enumerate(rows):
			if not isinstance(row, dict):
				raise frappe.ValidationError(f"SAP field '{source_array_field}' must contain JSON objects.")
			flattened.append(
				{
					**{f"__parent_{key}": value for key, value in parent_fields.items()},
					**parent_fields,
					**row,
					"__line_index": index,
				} if include_parent else row
			)
	return flattened


def _date_filter(
	creation_field: str | None,
	update_field: str | None,
	from_date: str | None,
	to_date: str | None,
) -> dict[str, str] | None:
	fields = list(dict.fromkeys(field for field in (creation_field, update_field) if field))
	if not fields:
		return None
	if not from_date and not to_date:
		return None
	if not from_date or not to_date:
		raise frappe.ValidationError("From Date and To Date are required for this mapping.")

	start = getdate(from_date)
	end = getdate(to_date)
	if start > end:
		raise frappe.ValidationError("From Date cannot be after To Date.")
	next_day = end + timedelta(days=1)

	conditions = [
		f"({field} ge '{start.isoformat()}' and {field} lt '{next_day.isoformat()}')"
		for field in fields
	]
	return {"$filter": conditions[0][1:-1] if len(conditions) == 1 else " or ".join(conditions)}
