"""Resolve SAP parent account codes to ERP Account names."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
	mapping = json.loads(doc.field_mapping)
	mapping["parent_account"] = catalog["SAP Account"]["field_mapping"]["parent_account"]
	doc.field_mapping = json.dumps(mapping, indent=2)
	doc.save(ignore_permissions=True)
