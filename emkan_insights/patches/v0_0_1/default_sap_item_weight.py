import json

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Item"):
		return
	mapping = frappe.get_doc("SAP Doctype Mapping", "SAP Item")
	fields = frappe.parse_json(mapping.field_mapping)
	if fields.get("weight_per_unit") != "InventoryWeight":
		return
	fields["weight_per_unit"] = {"fieldname": "InventoryWeight", "default": 0}
	mapping.field_mapping = json.dumps(fields, ensure_ascii=False)
	mapping.save(ignore_permissions=True)
