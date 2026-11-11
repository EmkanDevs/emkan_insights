"""Classify SAP at_Other accounts from their ERPNext parent or company root."""

import json

import frappe


def execute():
	name = "SAP Account"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	mapping = frappe.parse_json(doc.field_mapping or "{}")
	mapping["root_type"] = {
		"fieldname": "AccountType",
		"transform": "sap_account_root_type",
	}
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
