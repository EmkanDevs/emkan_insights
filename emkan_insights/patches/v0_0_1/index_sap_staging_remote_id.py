import frappe


def execute():
	"""Staging lookups filter by configuration and remote_id; without an index each batch scans the table."""
	if not frappe.db.table_exists("SAP Doctype Mapping"):
		return
	for doctype in frappe.get_all("SAP Doctype Mapping", pluck="external_doctype", distinct=True):
		if not doctype or not frappe.db.table_exists(doctype):
			continue
		meta = frappe.get_meta(doctype)
		if meta.has_field("external_sap_configuration") and meta.has_field("remote_id"):
			frappe.db.add_index(doctype, ["external_sap_configuration", "remote_id"], index_name="sap_cfg_remote_id")
