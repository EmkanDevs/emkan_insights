"""Install missing SAP mapping definitions from the bundled catalog."""

import json
from pathlib import Path

import frappe


def ensure_catalog_mappings():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = json.loads(path.read_text(encoding="utf-8"))
	existing = set(frappe.get_all("SAP Doctype Mapping", pluck="name"))

	for mapping in catalog:
		if mapping["name"] in existing:
			continue
		values = {**mapping, "field_mapping": json.dumps(mapping["field_mapping"], ensure_ascii=False)}
		doc = frappe.get_doc({"doctype": "SAP Doctype Mapping", **values})
		# Some target custom fields are site-specific; keep their definitions available for review.
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		existing.add(mapping["name"])
