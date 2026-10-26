import frappe


def execute():
	name = "SAP Account"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	mapping = frappe.get_doc("SAP Doctype Mapping", name)
	if mapping.excluded_sap_id_prefixes:
		return
	mapping.excluded_sap_id_prefixes = "7,9,A"
	mapping.save(ignore_permissions=True)
