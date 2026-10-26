import json

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS, _account_amount, _account_currency


NEW_SOURCES = {
	"SAP Stock Transfer Receipt Detail": ("StockTransfers", "Stock Ledger Entry", "External Stock Ledger Entry", "transfer-in:"),
	"SAP Payment Incoming Allocation Detail": ("IncomingPayments", "Payment Ledger Entry", "External Payment Ledger Entry", "incoming-allocation:"),
	"SAP Payment Outgoing Allocation Detail": ("VendorPayments", "Payment Ledger Entry", "External Payment Ledger Entry", "outgoing-allocation:"),
}


def execute():
	for name, definition in REVIEW_MAPPINGS.items():
		if name in NEW_SOURCES and not frappe.db.exists("SAP Doctype Mapping", name):
			_create_review_mapping(name, definition)
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		for fieldname in ("sap_filter", "source_array_field", "sap_id_field", "enabled"):
			if fieldname in definition or fieldname == "source_array_field":
				setattr(doc, fieldname, definition.get("source_array" if fieldname == "source_array_field" else fieldname))
		doc.field_mapping = json.dumps(definition["field_mapping"], indent=2)
		doc.save(ignore_permissions=True)

	_update_manual_journal_mapping()
	for name in frappe.get_all("External SAP Configuration", pluck="name"):
		frappe.get_doc("External SAP Configuration", name).save(ignore_permissions=True)


def _create_review_mapping(name, definition):
	endpoint, target, staging, prefix = NEW_SOURCES[name]
	frappe.get_doc({
		"doctype": "SAP Doctype Mapping",
		"mapping_name": name,
		"enabled": 1,
		"sap_doctype": endpoint,
		"erp_doctype": target,
		"external_doctype": staging,
		"sap_id_field": definition["sap_id_field"],
		"sap_id_prefix": prefix,
		"full_snapshot_sync": 1,
		"fetch_only": 1,
		"fetch_limit": 20,
		"field_mapping": json.dumps(definition["field_mapping"]),
	}).insert(ignore_permissions=True)


def _update_manual_journal_mapping():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Journal Entry"):
		return
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Journal Entry")
	doc.sap_filter = "OriginalJournal eq 'ttJournalEntry'"
	mapping = frappe.parse_json(doc.field_mapping)
	mapping["voucher_type"] = {"transform": "constant", "transform_config": {"value": "Journal Entry"}}
	mapping["accounts"]["mapping"].update({
		"cost_center": "CostingCode",
		"project": "ProjectCode",
		"account_currency": _account_currency(),
		"debit_in_account_currency": _account_amount("FCDebit", "Debit"),
		"credit_in_account_currency": _account_amount("FCCredit", "Credit"),
		"debit": "Debit",
		"credit": "Credit",
	})
	doc.field_mapping = json.dumps(mapping, indent=2)
	doc.save(ignore_permissions=True)
