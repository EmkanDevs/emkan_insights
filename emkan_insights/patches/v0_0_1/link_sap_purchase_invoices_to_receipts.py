"""Map SAP A/P invoice lines to their Goods Receipt PO lines."""

import json
from pathlib import Path

import frappe


def execute():
	name = "SAP Purchase Invoice"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(entry for entry in json.loads(path.read_text()) if entry["name"] == name)
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	fields = frappe.parse_json(doc.field_mapping)
	items = fields["items"]["mapping"]
	for field in ("purchase_receipt", "pr_detail"):
		items[field] = entry["field_mapping"]["items"]["mapping"][field]
	doc.field_mapping = json.dumps(fields, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
