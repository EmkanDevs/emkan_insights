"""Map verified SAP party-payment bank, cash, and control accounts."""

import json
from pathlib import Path

import frappe


PAYMENT_FIELDS = (
	"paid_from",
	"paid_to",
	"paid_from_account_currency",
	"paid_to_account_currency",
	"received_amount",
)


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	for name in ("SAP Payment Entry Incoming", "SAP Payment Entry Outgoing"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		mapping = json.loads(doc.field_mapping)
		mapping.update({field: catalog[name]["field_mapping"][field] for field in PAYMENT_FIELDS})
		doc.field_mapping = json.dumps(mapping, indent=2)
		doc.save(ignore_permissions=True)
