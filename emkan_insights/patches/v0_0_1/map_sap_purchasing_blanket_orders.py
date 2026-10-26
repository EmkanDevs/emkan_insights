"""Map SAP vendor blanket agreements as purchasing Blanket Orders."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Blanket Order"):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Blanket Order")
	mapping = json.loads(doc.field_mapping)
	mapping.pop("customer", None)
	for field in ("supplier", "blanket_order_type", "naming_series"):
		mapping[field] = catalog["SAP Blanket Order"][field]
	doc.field_mapping = json.dumps(mapping, indent=2)
	doc.save(ignore_permissions=True)
