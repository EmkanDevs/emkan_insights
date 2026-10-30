"""Map SAP item and account based purchase invoice lines."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text(encoding="utf-8"))}
	item_mapping = catalog["SAP Purchase Invoice"]["field_mapping"]["items"]
	for name in ("SAP Purchase Invoice", "SAP Purchase Down Payment"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		mapping = frappe.get_doc("SAP Doctype Mapping", name)
		fields = frappe.parse_json(mapping.field_mapping)
		fields["items"] = item_mapping
		mapping.field_mapping = json.dumps(fields, ensure_ascii=False, indent=2)
		mapping.save(ignore_permissions=True)
