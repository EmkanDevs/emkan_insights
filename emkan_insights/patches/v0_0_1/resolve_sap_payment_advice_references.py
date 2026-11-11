"""Resolve vendor payment advice allocations to their staged outgoing payments."""

import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	name = "SAP Payment Outgoing Allocation Detail"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	current = frappe.parse_json(doc.field_mapping or "{}")
	updated = REVIEW_MAPPINGS[name]["field_mapping"]
	current["against_voucher_type"] = updated["against_voucher_type"]
	current["against_voucher_no"] = updated["against_voucher_no"]
	doc.field_mapping = json.dumps(current, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
