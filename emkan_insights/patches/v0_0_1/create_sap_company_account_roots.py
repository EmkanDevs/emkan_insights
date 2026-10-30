"""Create the ERP root Accounts needed by an empty SAP target company."""

import frappe
from frappe.utils import now_datetime
from frappe.utils.nestedset import rebuild_tree

from erpnext.accounts.utils import get_autoname_with_number


ROOT_REPORT_TYPES = {
	"Asset": "Balance Sheet",
	"Liability": "Balance Sheet",
	"Equity": "Balance Sheet",
	"Income": "Profit and Loss",
	"Expense": "Profit and Loss",
}


def execute():
	companies = set(frappe.get_all("External SAP Configuration", pluck="company"))
	for company in sorted(companies):
		if frappe.db.count("Account", {"company": company}):
			continue
		now = now_datetime()
		rows = []
		for root_type, report_type in ROOT_REPORT_TYPES.items():
			rows.append((
				get_autoname_with_number(None, root_type, company), root_type, company,
				root_type, report_type, 1, now, now, "Administrator", "Administrator",
			))
		frappe.db.bulk_insert(
			"Account",
			fields=[
				"name", "account_name", "company", "root_type", "report_type",
				"is_group", "creation", "modified", "owner", "modified_by",
			],
			values=rows,
		)
		rebuild_tree("Account", "parent_account")
