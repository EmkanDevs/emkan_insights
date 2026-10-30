"""Align Item and Supplier links with the existing ERPNext master names."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	for name, fieldname in (("SAP Item", "item_group"), ("SAP Supplier", "supplier_group")):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		mapping[fieldname] = catalog[name][fieldname]
		doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
		doc.save(ignore_permissions=True)

	if not frappe.db.exists("UOM", "Nos"):
		frappe.get_doc({"doctype": "UOM", "uom_name": "Nos"}).insert(ignore_permissions=True)
