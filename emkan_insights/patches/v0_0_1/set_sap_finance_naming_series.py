"""Set the configured naming series on SAP finance mappings."""

import json
from pathlib import Path

import frappe


MAPPING_NAMES = (
	"SAP Purchase Invoice",
	"SAP Sales Invoice",
	"SAP Payment Entry Incoming",
	"SAP Payment Entry Outgoing",
)


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	for name in MAPPING_NAMES:
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		if mapping.get("naming_series"):
			continue
		mapping["naming_series"] = catalog[name]["field_mapping"]["naming_series"]
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
