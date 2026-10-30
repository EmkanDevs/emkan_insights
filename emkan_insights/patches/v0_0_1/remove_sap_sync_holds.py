import frappe


HOLD_REASON_KEY = "_sap_sync_hold_reason"


def execute():
	external_doctypes = set(frappe.get_all(
		"SAP Doctype Mapping",
		filters={"external_doctype": ["is", "set"]},
		pluck="external_doctype",
	))
	for doctype in external_doctypes:
		if not frappe.db.exists("DocType", doctype):
			continue
		_remove_json_hold_reasons(doctype)
		_clear_legacy_hold_column(doctype)


def _remove_json_hold_reasons(doctype: str) -> None:
	for row in frappe.get_all(
		doctype,
		filters={"json": ["like", f'%"{HOLD_REASON_KEY}"%']},
		fields=["name", "json"],
		limit_page_length=0,
	):
		payload = frappe.parse_json(row.json)
		if not isinstance(payload, dict) or HOLD_REASON_KEY not in payload:
			continue
		payload.pop(HOLD_REASON_KEY)
		frappe.db.set_value(doctype, row.name, "json", frappe.as_json(payload), update_modified=False)


def _clear_legacy_hold_column(doctype: str) -> None:
	if "sap_sync_hold_reason" not in frappe.db.get_table_columns(doctype):
		return
	table = frappe.qb.DocType(doctype)
	frappe.qb.update(table).set(table.sap_sync_hold_reason, None).run()
