import json
from pathlib import Path

import frappe


INCORRECT_DEPARTMENT_LOOKUP = {
	"fieldname": "Department",
	"transform": "sap_lookup",
	"transform_config": {
		"entity": "Departments",
		"key": "Code",
		"value_field": "Name",
		"key_type": "integer",
	},
}


def execute():
	name = "SAP Employee"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return

	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
	expected = next(mapping["field_mapping"] for mapping in catalog if mapping["name"] == name)
	mapping = frappe.get_doc("SAP Doctype Mapping", name)
	fields = frappe.parse_json(mapping.field_mapping)
	if fields.get("department") == INCORRECT_DEPARTMENT_LOOKUP:
		fields.pop("department")
	for fieldname in ("health_insurance_provider", "health_insurance_no"):
		fields.setdefault(fieldname, expected[fieldname])
	mapping.field_mapping = json.dumps(fields, ensure_ascii=False)
	mapping.save(ignore_permissions=True)
