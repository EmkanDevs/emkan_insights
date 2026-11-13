"""Add Abbr Required to SAP Doctype Mapping and copy it to configuration mapping rows."""

import frappe

# Mappings whose created names do not carry the company abbreviation.
NO_ABBR = {
	"SAP UOM", "SAP Customer Group", "SAP Bank", "SAP Mode of Payment", "SAP Supplier",
	"SAP Customer", "SAP Sales Stage", "SAP Lead Source", "SAP Designation",
	"SAP Manufacturer", "SAP Sales Tax Code", "SAP Territory",
}


def execute():
	frappe.reload_doc("sap", "doctype", "sap_doctype_mapping")
	for name in frappe.get_all("SAP Doctype Mapping", pluck="name"):
		frappe.db.set_value(
			"SAP Doctype Mapping", name, "abbr_required", 0 if name in NO_ABBR else 1,
			update_modified=False,
		)
	for row in frappe.get_all(
		"External Site Configuration CT",
		filters={"parenttype": "External SAP Configuration", "sap_mapping": ["is", "set"]},
		fields=["name", "sap_mapping"],
	):
		frappe.db.set_value(
			"External Site Configuration CT", row.name, "abbr_required",
			0 if row.sap_mapping in NO_ABBR else 1, update_modified=False,
		)
