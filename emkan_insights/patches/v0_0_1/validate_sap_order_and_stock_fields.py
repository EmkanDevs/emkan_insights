"""Supply verified order UOM and stock purpose fields."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	for name in ("SAP Sales Order", "SAP Stock Entry Issue", "SAP Stock Entry Receipt"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		source = catalog[name]
		if name == "SAP Sales Order":
			mapping["items"]["mapping"]["conversion_factor"] = source["items"]["mapping"]["conversion_factor"]
		else:
			mapping["purpose"] = source["purpose"]
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
