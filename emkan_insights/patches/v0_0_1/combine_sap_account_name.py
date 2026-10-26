import json

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		return
	mapping = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
	fields = frappe.parse_json(mapping.field_mapping)
	if fields.get("account_name") != "Name":
		return
	fields["account_name"] = {
		"transform": "join_fields",
		"transform_config": {"fields": ["Code", "Name"], "separator": " - ", "skip_missing": True},
	}
	mapping.field_mapping = json.dumps(fields, ensure_ascii=False)
	mapping.save(ignore_permissions=True)
