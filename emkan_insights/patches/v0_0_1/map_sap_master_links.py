"""Use ERP record names for partner and Asset Category links."""

import json
from pathlib import Path

import frappe


FIELDS = {
	"SAP Customer": ("customer_group", "default_price_list"),
	"SAP Supplier": ("supplier_group", "default_price_list"),
	"SAP Item": ("asset_category",),
}


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	for name, fields in FIELDS.items():
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		for field in fields:
			mapping[field] = catalog[name][field]
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
