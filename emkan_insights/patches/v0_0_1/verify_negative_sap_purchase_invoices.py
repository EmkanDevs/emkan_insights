"""Verify negative SAP purchase invoice ledger amounts against posted journals."""

import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	name = "SAP Payment Purchase Invoice Detail"
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	doc.field_mapping = json.dumps(REVIEW_MAPPINGS[name]["field_mapping"], ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
