import json

import frappe


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		return
	mapping = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
	fields = frappe.parse_json(mapping.field_mapping)
	rule = fields.get("account_name")
	if not isinstance(rule, dict) or rule.get("transform") != "join_fields":
		return
	config = rule.get("transform_config") or {}
	if config.get("fields") != ["Code", "Name"] or config.get("skip_missing"):
		return
	config["skip_missing"] = True
	rule["transform_config"] = config
	mapping.field_mapping = json.dumps(fields, ensure_ascii=False)
	mapping.save(ignore_permissions=True)
