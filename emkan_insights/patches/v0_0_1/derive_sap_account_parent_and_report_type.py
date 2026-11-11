"""Use the SAP parent key and mapped root type for Account staging."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(row for row in json.loads(path.read_text()) if row["name"] == "SAP Account")
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
	mapping = frappe.parse_json(doc.field_mapping)
	for field in ("parent_account", "account_type", "root_type", "report_type"):
		mapping[field] = entry["field_mapping"][field]
	if mapping == frappe.parse_json(doc.field_mapping):
		return
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
