"""Create the ERP company root for SAP cost center children."""

import frappe
from frappe.utils import now_datetime
from frappe.utils.nestedset import rebuild_tree

from erpnext.accounts.utils import get_autoname_with_number


def execute():
	companies = set(frappe.get_all("External SAP Configuration", pluck="company"))
	for company in sorted(companies):
		root_name = _get_root(company)
		if not root_name:
			root_name = get_autoname_with_number(None, company, company)
			now = now_datetime()
			frappe.db.bulk_insert(
				"Cost Center",
				fields=[
					"name", "cost_center_name", "company", "is_group",
					"creation", "modified", "owner", "modified_by",
				],
				values=[(root_name, company, company, 1, now, now, "Administrator", "Administrator")],
			)
		default_name = frappe.db.get_value("Company", company, "cost_center")
		if default_name and default_name != root_name and frappe.db.exists("Cost Center", default_name):
			frappe.db.set_value(
				"Cost Center", default_name, "parent_cost_center", root_name,
				update_modified=False,
			)
		rebuild_tree("Cost Center", "parent_cost_center")


def _get_root(company: str) -> str | None:
	roots = frappe.get_all(
		"Cost Center",
		filters={
			"company": company,
			"cost_center_name": company,
			"parent_cost_center": ["is", "not set"],
			"is_group": 1,
		},
		pluck="name",
		limit_page_length=2,
	)
	if len(roots) > 1:
		raise frappe.ValidationError(f"Company {company} has more than one root Cost Center.")
	return roots[0] if roots else None
