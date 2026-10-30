"""Map SAP payment methods to complete ERPNext Modes of Payment."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Mode of Payment"):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Mode of Payment")
	doc.field_mapping = json.dumps(
		catalog["SAP Mode of Payment"]["field_mapping"], ensure_ascii=False, indent=2
	)
	doc.save(ignore_permissions=True)
