"""Map SAP Delivery Notes to complete ERPNext Delivery Notes."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Delivery Note"):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Delivery Note")
	doc.field_mapping = json.dumps(
		catalog["SAP Delivery Note"]["field_mapping"], ensure_ascii=False, indent=2
	)
	doc.save(ignore_permissions=True)
