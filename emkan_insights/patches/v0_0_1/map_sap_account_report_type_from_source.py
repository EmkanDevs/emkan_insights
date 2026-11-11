"""Map SAP Account report type from AccountType, without changing other rules."""

import json
from pathlib import Path

import frappe


def execute():
	name = "SAP Account"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(row for row in json.loads(path.read_text(encoding="utf-8")) if row["name"] == name)
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	mapping = frappe.parse_json(doc.field_mapping)
	new_rule = entry["field_mapping"]["report_type"]
	if mapping.get("report_type") == new_rule:
		return
	mapping["report_type"] = new_rule
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
