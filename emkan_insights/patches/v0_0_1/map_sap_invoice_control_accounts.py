"""Map SAP invoice control accounts to ERP receivable and payable accounts."""

import json
from pathlib import Path

import frappe


def execute():
	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(catalog_path.read_text())}
	for name, target_field in (
		("SAP Purchase Invoice", "credit_to"),
		("SAP Sales Invoice", "debit_to"),
	):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		if mapping.get(target_field):
			continue
		mapping[target_field] = catalog[name]["field_mapping"][target_field]
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
