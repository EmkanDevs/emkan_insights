"""Use ERP Warehouse and Account names in Item defaults."""

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
		fields = ("default_warehouse",) if name == "SAP Item" else ("expense_account", "income_account")
		table = "item_defaults" if name == "SAP Item" else "item_group_defaults"
		for field in fields:
			mapping[table]["mapping"][field] = catalog[name][table]["mapping"][field]
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
