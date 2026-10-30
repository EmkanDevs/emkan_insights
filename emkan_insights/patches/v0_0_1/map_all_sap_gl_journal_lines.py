"""Stage manual and generated SAP journal lines as ERPNext GL Entry payloads."""

import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	if frappe.db.exists("SAP Doctype Mapping", "SAP Journal Entry"):
		journal_mapping = frappe.get_doc("SAP Doctype Mapping", "SAP Journal Entry")
		journal_mapping.sap_filter = None
		journal_mapping.save(ignore_permissions=True)

	if not frappe.db.exists("SAP Doctype Mapping", "SAP GL Journal Detail"):
		return

	definition = REVIEW_MAPPINGS["SAP GL Journal Detail"]
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP GL Journal Detail")
	doc.field_mapping = json.dumps(definition["field_mapping"], ensure_ascii=False, indent=2)
	doc.sap_filter = definition["sap_filter"]
	doc.fetch_limit = 0
	doc.save(ignore_permissions=True)
