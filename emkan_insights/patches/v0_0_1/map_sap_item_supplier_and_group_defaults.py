"""Use Supplier codes and omit SAP Item Group warehouse enum values."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	for name in ("SAP Item", "SAP Item Group"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		if name == "SAP Item":
			for table, field in (("item_defaults", "default_supplier"), ("supplier_items", "supplier")):
				mapping[table]["mapping"][field] = catalog[name][table]["mapping"][field]
		else:
			mapping["item_group_defaults"]["mapping"].pop("default_warehouse", None)
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
