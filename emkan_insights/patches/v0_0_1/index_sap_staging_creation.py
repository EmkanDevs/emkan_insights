import frappe


def execute():
	"""Sync pages pending staging rows by (creation, name) within one configuration."""
	if not frappe.db.table_exists("SAP Doctype Mapping"):
		return
	for doctype in frappe.get_all("SAP Doctype Mapping", pluck="external_doctype", distinct=True):
		if doctype and frappe.db.table_exists(doctype) and frappe.get_meta(doctype).has_field("external_sap_configuration"):
			frappe.db.add_index(doctype, ["external_sap_configuration", "creation"], index_name="sap_cfg_creation")
