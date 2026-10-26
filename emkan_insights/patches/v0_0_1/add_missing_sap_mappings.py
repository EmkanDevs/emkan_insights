import json
from pathlib import Path

import frappe


MAPPING_NAMES = {
	"SAP Bank Account",
	"SAP Department",
	"SAP Territory",
	"SAP Sales Stage",
	"SAP Sales Tax Code",
	"SAP Inventory Transfer Request",
	"SAP Inventory Posting",
}


def execute():
	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	for mapping in json.loads(catalog_path.read_text(encoding="utf-8")):
		if mapping["name"] == "SAP Employee" and frappe.db.exists("SAP Doctype Mapping", mapping["name"]):
			frappe.db.set_value(
				"SAP Doctype Mapping",
				mapping["name"],
				{"creation_date_filter_field": "CreateDate", "full_snapshot_sync": 0},
			)
		if mapping["name"] not in MAPPING_NAMES:
			continue
		if frappe.db.exists("SAP Doctype Mapping", mapping["name"]):
			continue
		values = {**mapping, "field_mapping": json.dumps(mapping["field_mapping"], ensure_ascii=False)}
		frappe.get_doc({"doctype": "SAP Doctype Mapping", **values}).insert(ignore_permissions=True)

	for configuration_name in frappe.get_all("External SAP Configuration", pluck="name"):
		configuration = frappe.get_doc("External SAP Configuration", configuration_name)
		configuration.save(ignore_permissions=True)
