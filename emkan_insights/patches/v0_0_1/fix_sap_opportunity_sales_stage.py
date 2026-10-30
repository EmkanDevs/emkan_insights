"""Resolve SAP opportunity stages by the ERP stage label."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Opportunity"):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text(encoding="utf-8"))}
	mapping = frappe.get_doc("SAP Doctype Mapping", "SAP Opportunity")
	fields = frappe.parse_json(mapping.field_mapping)
	fields["sales_stage"] = catalog["SAP Opportunity"]["field_mapping"]["sales_stage"]
	mapping.field_mapping = json.dumps(fields, ensure_ascii=False, indent=2)
	mapping.save(ignore_permissions=True)
