from __future__ import annotations

import re
from datetime import timedelta

import frappe
from frappe.utils import cint, flt, getdate

from emkan_insights.sap.services.client import SAPB1Client


def fetch(
	client: SAPB1Client,
	mapping_doc,
	from_date: str | None = None,
	to_date: str | None = None,
	cursor_after: int | None = None,
) -> list[dict]:
	params = _query_params(mapping_doc, from_date, to_date, cursor_after)
	limit = cint(getattr(mapping_doc, "fetch_limit", 0))
	if limit:
		params = {**(params or {}), "$top": str(limit)}
		records = client.fetch_all(mapping_doc.sap_doctype, params=params, max_records=limit)
	elif (workers := cint(getattr(mapping_doc, "fetch_workers", 0))) > 1:
		records = client.fetch_all(
			mapping_doc.sap_doctype, params=params,
			workers=workers, order_by=mapping_doc.sap_id_field.split("+")[0].strip(),
		)
	else:
		records = client.fetch_all(mapping_doc.sap_doctype, params=params)
	flattened = _flatten_records(
		records, mapping_doc.source_array_field,
		include_parent="+" in mapping_doc.sap_id_field,
	)
	if mapping_doc.name == "SAP GL Journal Detail":
		return [row for row in flattened if flt(row.get("Debit")) or flt(row.get("Credit"))]
	return flattened


def _query_params(
	mapping_doc, from_date: str | None, to_date: str | None, cursor_after: int | None = None,
) -> dict[str, str] | None:
	filters = []
	if mapping_doc.sap_filter:
		filters.append(f"({mapping_doc.sap_filter.strip()})")

	date_params = None
	cursor_field = getattr(mapping_doc, "cursor_field", None)
	if cursor_field and not getattr(mapping_doc, "full_snapshot_sync", False):
		if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cursor_field):
			raise frappe.ValidationError(f"Invalid SAP cursor field '{cursor_field}'.")
		# Without a cursor (first run, or a forced refresh) every record is fetched.
		if cursor_after is not None:
			filters.append(f"({cursor_field} gt {int(cursor_after)})")
	elif not getattr(mapping_doc, "full_snapshot_sync", False):
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

	params = {"$filter": " and ".join(filters)} if filters else {}
	if select := _select_fields(mapping_doc):
		params["$select"] = select
	return params or None


def _select_fields(mapping_doc) -> str | None:
	raw = getattr(mapping_doc, "sap_select_fields", None)
	fields = [field.strip() for field in (raw or "").replace("\n", ",").split(",") if field.strip()]
	if not fields:
		return None
	for field in fields:
		if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
			raise frappe.ValidationError(f"Invalid SAP select field '{field}'.")
	return ",".join(dict.fromkeys(fields))


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
		parent_context = {f"__parent_{key}": value for key, value in record.items()}
		for index, row in enumerate(rows):
			if not isinstance(row, dict):
				raise frappe.ValidationError(f"SAP field '{source_array_field}' must contain JSON objects.")
			flattened.append(
				{
					**parent_context,
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
