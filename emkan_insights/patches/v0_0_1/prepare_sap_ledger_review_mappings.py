import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	for name, definition in REVIEW_MAPPINGS.items():
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		if not doc.raw_payload or not doc.fetch_only:
			continue
		if frappe.parse_json(doc.field_mapping or "{}"):
			continue
		doc.field_mapping = json.dumps(definition["field_mapping"], indent=2)
		doc.save(ignore_permissions=True)
