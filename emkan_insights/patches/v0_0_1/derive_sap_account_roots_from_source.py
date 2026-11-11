"""Classify new company charts from verified SAP root codes."""

import json
from pathlib import Path

import frappe


def execute():
	name = "SAP Account"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(row for row in json.loads(path.read_text()) if row["name"] == name)
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	mapping = frappe.parse_json(doc.field_mapping or "{}")
	for field in ("root_type", "account_type"):
		mapping[field] = entry["field_mapping"][field]
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
