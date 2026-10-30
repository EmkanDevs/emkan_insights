"""Map SAP Profit Centers to complete ERPNext Cost Centers."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Cost Center"):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Cost Center")
	doc.field_mapping = json.dumps(
		catalog["SAP Cost Center"]["field_mapping"], ensure_ascii=False, indent=2
	)
	doc.save(ignore_permissions=True)
