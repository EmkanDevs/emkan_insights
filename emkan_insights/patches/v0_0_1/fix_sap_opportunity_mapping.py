"""Apply the verified SAP Opportunity field mapping."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Opportunity"):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(entry for entry in json.loads(path.read_text()) if entry["name"] == "SAP Opportunity")
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Opportunity")
	doc.field_mapping = json.dumps(entry["field_mapping"], ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
