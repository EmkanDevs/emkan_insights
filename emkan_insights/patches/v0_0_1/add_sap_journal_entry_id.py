"""Store the SAP journal number on submitted ERPNext Journal Entries."""

import frappe


def execute():
	name = "Journal Entry-custom_sap_jdt_num"
	if frappe.db.exists("Custom Field", name):
		return

	frappe.get_doc({
		"doctype": "Custom Field",
		"dt": "Journal Entry",
		"fieldname": "custom_sap_jdt_num",
		"label": "SAP Journal Entry ID",
		"fieldtype": "Data",
		"insert_after": "user_remark",
		"unique": 1,
		"read_only": 1,
		"no_copy": 1,
	}).insert(ignore_permissions=True)
