"""Apply SAP Purchase Request links and stock quantities to purchase mappings."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entries = {entry["name"]: entry for entry in json.loads(path.read_text())}
	for name in ("SAP Material Request", "SAP Purchase Order"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		doc.field_mapping = json.dumps(entries[name]["field_mapping"], ensure_ascii=False, indent=2)
		doc.save(ignore_permissions=True)
