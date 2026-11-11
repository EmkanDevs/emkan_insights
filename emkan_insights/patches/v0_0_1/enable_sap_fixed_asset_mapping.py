"""Enable Asset imports from fixed SAP Items after ERPNext Items are available."""

import json
from pathlib import Path

import frappe


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	mapping = next(entry for entry in json.loads(path.read_text()) if entry["name"] == "SAP Fixed Asset")
	values = {**mapping, "field_mapping": json.dumps(mapping["field_mapping"], ensure_ascii=False)}
	if frappe.db.exists("SAP Doctype Mapping", mapping["name"]):
		doc = frappe.get_doc("SAP Doctype Mapping", mapping["name"])
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({"doctype": "SAP Doctype Mapping", **values}).insert(ignore_permissions=True)

	for name in frappe.get_all("External SAP Configuration", pluck="name"):
		configuration = frappe.get_doc("External SAP Configuration", name)
		if any(row.sap_mapping == mapping["name"] for row in configuration.mappings):
			continue
		configuration.append("mappings", {
			"sap_mapping": mapping["name"],
			"ref_doctype": "Asset",
			"exported_doctype": "External Asset",
		})
		configuration.save(ignore_permissions=True)
