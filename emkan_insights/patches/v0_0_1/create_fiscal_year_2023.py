"""Create Fiscal Year 2023: Saad Ali SAP data starts with 2023-01-01 opening balances."""

import frappe


def execute():
	if frappe.db.exists("Fiscal Year", "2023"):
		return
	frappe.get_doc({
		"doctype": "Fiscal Year",
		"year": "2023",
		"year_start_date": "2023-01-01",
		"year_end_date": "2023-12-31",
	}).insert(ignore_permissions=True)
