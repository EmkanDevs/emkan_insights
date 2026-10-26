"""Sum SAP payment methods instead of taking a zero-valued first field."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	for name in ("SAP Payment Entry Incoming", "SAP Payment Entry Outgoing"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		if mapping.get("paid_amount", {}).get("transform") != "first_value":
			continue
		mapping["paid_amount"] = catalog[name]["field_mapping"]["paid_amount"]
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
