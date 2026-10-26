"""Map SAP invoice line UOM conversion from ERP Item units."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	for name in ("SAP Purchase Invoice", "SAP Sales Invoice"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		mapping["items"]["mapping"]["conversion_factor"] = (
			catalog[name]["field_mapping"]["items"]["mapping"]["conversion_factor"]
		)
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
