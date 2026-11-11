"""Keep source dates while mapping SAP requests and calendar-month terms."""

import json
from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from emkan_insights.sap.payment_dates import INVOICE_MONTH_BASIS


MAPPINGS = ("SAP Material Request", "SAP Payment Term")
PAYMENT_DATE_DOCTYPES = ("Payment Term", "Payment Terms Template Detail", "Payment Schedule")


def execute():
	create_custom_fields({
		"Material Request": [{
			"fieldname": "custom_sap_due_date",
			"label": "SAP Due Date",
			"fieldtype": "Date",
			"insert_after": "schedule_date",
			"read_only": 1,
		}],
		"Material Request Item": [{
			"fieldname": "custom_sap_required_date",
			"label": "SAP Required Date",
			"fieldtype": "Date",
			"insert_after": "schedule_date",
			"read_only": 1,
		}],
	})

	for doctype in PAYMENT_DATE_DOCTYPES:
		_add_invoice_month_option(doctype)
	_add_month_field_visibility()
	_update_mappings()


def _add_invoice_month_option(doctype):
	field = frappe.get_meta(doctype).get_field("due_date_based_on")
	options = (field.options or "").splitlines()
	if INVOICE_MONTH_BASIS not in options:
		options.append(INVOICE_MONTH_BASIS)
		make_property_setter(doctype, "due_date_based_on", "options", "\n".join(options), "Text")


def _add_month_field_visibility():
	for doctype in PAYMENT_DATE_DOCTYPES:
		field = frappe.get_meta(doctype).get_field("credit_months")
		depends_on = field.depends_on or ""
		if INVOICE_MONTH_BASIS in depends_on:
			continue
		expression = depends_on.removeprefix("eval:") or "False"
		value = f"eval:({expression}) or doc.due_date_based_on == '{INVOICE_MONTH_BASIS}'"
		make_property_setter(doctype, "credit_months", "depends_on", value, "Data")


def _update_mappings():
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text(encoding="utf-8"))}
	for name in MAPPINGS:
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		field_mapping = json.dumps(catalog[name]["field_mapping"], ensure_ascii=False, indent=2)
		if json.loads(doc.field_mapping) == json.loads(field_mapping):
			continue
		doc.field_mapping = field_mapping
		doc.save(ignore_permissions=True)
