"""Point SAP GL lines at the imported document that posted them instead of the Journal Entry."""

import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP GL Journal Detail"):
		return
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP GL Journal Detail")
	mapping = json.loads(doc.field_mapping)
	definition = REVIEW_MAPPINGS["SAP GL Journal Detail"]["field_mapping"]
	mapping["voucher_type"] = definition["voucher_type"]
	mapping["voucher_no"] = definition["voucher_no"]
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
