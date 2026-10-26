"""Set ERP account types for verified SAP invoice control accounts."""

import json
from pathlib import Path

import frappe


ACCOUNT_ROLES = {
	"1131101": ("Receivable", "Asset"),
	"1131105": ("Receivable", "Asset"),
	"2131101": ("Payable", "Liability"),
	"2131102": ("Payable", "Liability"),
}


def execute():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	if frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		mapping_doc = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
		mapping = json.loads(mapping_doc.field_mapping)
		mapping["account_type"] = catalog["SAP Account"]["field_mapping"]["account_type"]
		mapping_doc.field_mapping = json.dumps(mapping, indent=2)
		mapping_doc.save(ignore_permissions=True)

	company = frappe.db.get_value("External SAP Configuration", "Emkan", "company")
	if not company:
		return
	for number, (account_type, root_type) in ACCOUNT_ROLES.items():
		name = frappe.db.get_value(
			"Account", {"account_number": number, "company": company}, "name"
		)
		if not name:
			continue
		account = frappe.get_doc("Account", name)
		if account.root_type != root_type or account.is_group or account.disabled:
			raise frappe.ValidationError("A SAP invoice control account has an unexpected ERP setup.")
		if account.account_type not in (None, "", account_type):
			raise frappe.ValidationError("A SAP invoice control account has a conflicting ERP type.")
		if account.account_type != account_type:
			# Existing SAP-imported parent links are unresolved, so Account.save cannot validate them.
			frappe.db.set_value("Account", name, "account_type", account_type)
			frappe.clear_document_cache("Account", name)
