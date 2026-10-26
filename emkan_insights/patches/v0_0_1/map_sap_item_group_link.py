"""Map SAP Item group code to the ERP Item Group record name."""

import json
from pathlib import Path

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Item"):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry["field_mapping"] for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Item")
	mapping = json.loads(doc.field_mapping)
	mapping["item_group"] = catalog["SAP Item"]["item_group"]
	doc.field_mapping = json.dumps(mapping, indent=2)
	doc.save(ignore_permissions=True)
