"""Map customer refunds in SAP outgoing payments."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Payment Entry Outgoing"):
		return

	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(entry for entry in json.loads(path.read_text()) if entry["name"] == "SAP Payment Entry Outgoing")
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Payment Entry Outgoing")
	doc.field_mapping = json.dumps(entry["field_mapping"], ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
