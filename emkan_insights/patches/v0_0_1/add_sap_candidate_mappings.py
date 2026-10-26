import json
from pathlib import Path

import frappe


MAPPING_NAMES = {
	"SAP Designation",
	"SAP Lead Source",
	"SAP Mode of Payment",
	"SAP Location",
	"SAP Opportunity",
	"SAP BOM",
}


def execute():
	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	for mapping in json.loads(catalog_path.read_text(encoding="utf-8")):
		if mapping["name"] not in MAPPING_NAMES:
			continue
		if frappe.db.exists("SAP Doctype Mapping", mapping["name"]):
			previous_fields = {
				"SAP BOM": {"item": "TreeCode", "quantity": "Quantity"},
				"SAP Mode of Payment": {"mode_of_payment": "Description"},
			}
			if mapping["name"] in previous_fields:
				existing = frappe.get_doc("SAP Doctype Mapping", mapping["name"])
				if json.loads(existing.field_mapping) == previous_fields[mapping["name"]]:
					existing.field_mapping = json.dumps(mapping["field_mapping"], ensure_ascii=False)
					existing.save(ignore_permissions=True)
			continue
		values = {**mapping, "field_mapping": json.dumps(mapping["field_mapping"], ensure_ascii=False)}
		frappe.get_doc({"doctype": "SAP Doctype Mapping", **values}).insert(ignore_permissions=True)
