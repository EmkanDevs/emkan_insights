"""Add SAP chart verified income and expense branch rules to the Account mapping."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
	mapping = frappe.parse_json(doc.field_mapping)
	mapping["root_type"] = catalog["SAP Account"]["root_type"]
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
