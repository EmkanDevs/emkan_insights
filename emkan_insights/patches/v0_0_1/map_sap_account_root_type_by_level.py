"""Use configured AccountLevel rules for both stored SAP Account classifications."""

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
	mapping = frappe.parse_json(doc.field_mapping or "{}")
	rules = entry["field_mapping"]
	if all(mapping.get(field) == rules[field] for field in ("root_type", "report_type")):
		return
	for field in ("root_type", "report_type"):
		mapping[field] = rules[field]
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
