import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields({
		"Item": [{
			"fieldname": "custom_sap_full_item_name",
			"label": "SAP Full Item Name",
			"fieldtype": "Small Text",
			"insert_after": "item_name",
			"read_only": 1,
		}],
	})

	if not frappe.db.exists("SAP Doctype Mapping", "SAP Item"):
		return
	mapping = frappe.get_doc("SAP Doctype Mapping", "SAP Item")
	fields = frappe.parse_json(mapping.field_mapping)
	if fields.get("item_name") != "ItemName":
		return
	fields["item_name"] = {
		"fieldname": "ItemName",
		"transform": "truncate",
		"transform_config": {"max_length": 140},
	}
	fields.setdefault("custom_sap_full_item_name", "ItemName")
	mapping.field_mapping = json.dumps(fields, ensure_ascii=False)
	mapping.save(ignore_permissions=True)
