"""Index SAP staging lookups by configuration and source ID."""

import frappe


def execute():
	doctypes = set(frappe.get_all("SAP Doctype Mapping", pluck="external_doctype"))
	for doctype in sorted(doctypes):
		if not doctype or not frappe.db.table_exists(doctype):
			continue
		meta = frappe.get_meta(doctype)
		if not meta.has_field("external_sap_configuration") or not meta.has_field("remote_id"):
			continue
		frappe.db.add_index(
			doctype,
			["external_sap_configuration", "remote_id"],
			"sap_configuration_remote_id",
		)
