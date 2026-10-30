"""Stage complete SAP payment-ledger rows with valid external voucher links."""

import json
from pathlib import Path

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


PAYMENT_LEDGER_MAPPINGS = (
	"SAP Payment Incoming Detail",
	"SAP Payment Outgoing Detail",
	"SAP Payment Purchase Invoice Detail",
	"SAP Payment Sales Invoice Detail",
	"SAP Payment Incoming Allocation Detail",
	"SAP Payment Outgoing Allocation Detail",
	"SAP Payment Purchase Down Payment Detail",
)


def execute():
	purchase_invoice_mapping = _purchase_invoice_field_mapping()
	_create_mapping(
		"SAP Purchase Down Payment",
		"PurchaseDownPayments",
		"Purchase Invoice",
		"External Purchase Invoice",
		"down-payment:",
		purchase_invoice_mapping,
	)
	_create_mapping(
		"SAP Payment Purchase Down Payment Detail",
		"PurchaseDownPayments",
		"Payment Ledger Entry",
		"External Payment Ledger Entry",
		"purchase-down-payment:",
		REVIEW_MAPPINGS["SAP Payment Purchase Down Payment Detail"]["field_mapping"],
	)

	for name in PAYMENT_LEDGER_MAPPINGS:
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		doc.field_mapping = json.dumps(
			REVIEW_MAPPINGS[name]["field_mapping"], ensure_ascii=False, indent=2
		)
		doc.fetch_limit = 0
		doc.save(ignore_permissions=True)

	for name in frappe.get_all("External SAP Configuration", pluck="name"):
		frappe.get_doc("External SAP Configuration", name).save(ignore_permissions=True)


def _purchase_invoice_field_mapping():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	entry = next(
		entry for entry in json.loads(path.read_text()) if entry["name"] == "SAP Purchase Invoice"
	)
	return entry["field_mapping"]


def _create_mapping(name, endpoint, target, staging, prefix, field_mapping):
	if frappe.db.exists("SAP Doctype Mapping", name):
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		doc.field_mapping = json.dumps(field_mapping, ensure_ascii=False, indent=2)
		doc.save(ignore_permissions=True)
		return
	frappe.get_doc({
		"doctype": "SAP Doctype Mapping",
		"mapping_name": name,
		"enabled": 1,
		"sap_doctype": endpoint,
		"erp_doctype": target,
		"external_doctype": staging,
		"sap_id_field": "DocEntry",
		"sap_id_prefix": prefix,
		"full_snapshot_sync": 1,
		"fetch_only": 1,
		"raw_payload": 0,
		"fetch_limit": 0,
		"field_mapping": json.dumps(field_mapping, ensure_ascii=False, indent=2),
	}).insert(ignore_permissions=True)
