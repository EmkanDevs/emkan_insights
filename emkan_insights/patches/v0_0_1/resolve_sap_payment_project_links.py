"""Resolve SAP payment Project codes to company Project names."""

import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	for name, definition in REVIEW_MAPPINGS.items():
		mapping = definition.get("field_mapping", {})
		project = mapping.get("project")
		if not isinstance(project, dict) or project.get("transform") != "sap_project_link":
			continue
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		current = frappe.parse_json(doc.field_mapping or "{}")
		current["project"] = mapping["project"]
		doc.field_mapping = json.dumps(current, ensure_ascii=False, indent=2)
		doc.save(ignore_permissions=True)
