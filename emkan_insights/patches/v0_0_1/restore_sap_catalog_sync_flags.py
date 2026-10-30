"""Restore target synchronization for normal SAP catalog mappings."""

import json
from pathlib import Path

import frappe
from frappe.utils import cint


def execute():
	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	for mapping in json.loads(catalog_path.read_text(encoding="utf-8")):
		if not frappe.db.exists("SAP Doctype Mapping", mapping["name"]):
			continue
		expected = cint(mapping.get("fetch_only", 0))
		current = cint(frappe.db.get_value("SAP Doctype Mapping", mapping["name"], "fetch_only"))
		if current == expected:
			continue
		frappe.db.set_value(
			"SAP Doctype Mapping",
			mapping["name"],
			"fetch_only",
			expected,
			update_modified=False,
		)
