"""Enable scoped direct import of SAP purchase down payments."""

import frappe


def execute():
	if frappe.db.exists("SAP Doctype Mapping", "SAP Purchase Down Payment"):
		frappe.db.set_value(
			"SAP Doctype Mapping", "SAP Purchase Down Payment", "fetch_only", 0,
			update_modified=False,
		)
