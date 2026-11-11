"""Keep SAP invoice-month terms accurate in ERPNext payment schedules."""

import frappe
from frappe.utils import add_days, add_months, cint, get_last_day, getdate

from erpnext.accounts import party
from erpnext.controllers import accounts_controller


INVOICE_MONTH_BASIS = "Month(s) after invoice date"
_original_get_due_date = accounts_controller.get_due_date
_original_template_due_date = party.get_due_date_from_template


def get_due_date(term, posting_date=None, bill_date=None):
	if term.due_date_based_on == INVOICE_MONTH_BASIS:
		date = bill_date or posting_date
		return add_months(date, cint(term.credit_months)) if date else None
	return _original_get_due_date(term, posting_date, bill_date)


def get_due_date_from_template(template_name, posting_date, bill_date):
	terms = frappe.get_doc("Payment Terms Template", template_name).terms
	if not any(term.due_date_based_on == INVOICE_MONTH_BASIS for term in terms):
		return _original_template_due_date(template_name, posting_date, bill_date)

	due_date = getdate(bill_date or posting_date)
	for term in terms:
		if term.due_date_based_on == INVOICE_MONTH_BASIS:
			candidate = add_months(due_date, cint(term.credit_months))
		elif term.due_date_based_on == "Day(s) after invoice date":
			candidate = add_days(due_date, cint(term.credit_days))
		elif term.due_date_based_on == "Day(s) after the end of the invoice month":
			candidate = add_days(get_last_day(due_date), cint(term.credit_days))
		else:
			candidate = get_last_day(add_months(due_date, cint(term.credit_months)))
		due_date = max(due_date, getdate(candidate))
	return due_date


def install():
	accounts_controller.get_due_date = get_due_date
	party.get_due_date_from_template = get_due_date_from_template
