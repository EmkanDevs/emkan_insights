"""Align SAP stock entries and stock ledger voucher references."""

import json
from pathlib import Path

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	for name in ("SAP Stock Entry Issue", "SAP Stock Entry Receipt"):
		_update_mapping(name, catalog[name]["field_mapping"])
	for name in ("SAP Stock Goods Issue Detail", "SAP Stock Goods Receipt Detail"):
		_update_mapping(name, REVIEW_MAPPINGS[name]["field_mapping"])


def _update_mapping(name, field_mapping):
	if not frappe.db.exists("SAP Doctype Mapping", name):
		return
	doc = frappe.get_doc("SAP Doctype Mapping", name)
	doc.field_mapping = json.dumps(field_mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
