"""Map incoming SAP account receipts and party receipts to ERPNext payments."""

import json
from pathlib import Path

import frappe


def execute():
	name = "SAP Payment Entry Incoming"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	doc.field_mapping = json.dumps(catalog[name]["field_mapping"], ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
