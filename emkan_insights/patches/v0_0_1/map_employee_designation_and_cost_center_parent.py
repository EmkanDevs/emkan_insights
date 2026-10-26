"""Resolve Employee designations and stop using SAP group codes as parents."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	for name in ("SAP Employee", "SAP Cost Center"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		if name == "SAP Employee":
			mapping["designation"] = catalog[name]["designation"]
		else:
			mapping.pop("parent_cost_center", None)
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
