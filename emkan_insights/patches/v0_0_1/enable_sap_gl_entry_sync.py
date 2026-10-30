"""Enable direct GL Entry synchronization after ledger mapping review."""

import frappe


def execute():
	if frappe.db.exists("SAP Doctype Mapping", "SAP GL Journal Detail"):
		frappe.db.set_value(
			"SAP Doctype Mapping", "SAP GL Journal Detail", "fetch_only", 0,
			update_modified=False,
		)
