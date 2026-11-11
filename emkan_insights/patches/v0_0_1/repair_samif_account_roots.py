"""Reconnect Samif SAP branches to the existing ERPNext company roots."""

import frappe
from frappe.utils.nestedset import rebuild_tree


ROOT_TYPES = ("Asset", "Liability", "Equity", "Income", "Expense")
COMPANY = "Samif"


def execute():
	if not frappe.db.exists("Company", COMPANY):
		return

	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	accounts = frappe.get_all(
		"Account",
		filters={"company": COMPANY},
		fields=["name", "account_number", "parent_account", "root_type", "is_group", "lft", "rgt"],
		limit_page_length=0,
	)
	if not accounts:
		return

	by_name = {account.name: account for account in accounts}
	roots = {root_type: f"{root_type} - {abbr}" for root_type in ROOT_TYPES}
	if not any(name in by_name for name in roots.values()):
		# A site already converted to SAP chart roots has no ERPNext company roots
		# left to repair. Still reject broken links or an invalid tree.
		_verify_samif_tree(len(accounts), roots=None)
		return
	for root_type, name in roots.items():
		root = by_name.get(name)
		if not root or root.parent_account or root.account_number or not root.is_group or root.root_type != root_type:
			frappe.throw(f"Samif {root_type} root Account is missing or invalid: {name}")

	repairs = []
	for account in accounts:
		if not account.parent_account or account.parent_account in by_name:
			continue
		expected = f"{abbr}-{roots.get(account.root_type, '')}"
		if account.parent_account != expected:
			frappe.throw(f"Unexpected Samif Account parent: {account.name} -> {account.parent_account}")
		repairs.append((account.name, roots[account.root_type]))

	for name, parent in repairs:
		frappe.db.set_value("Account", name, "parent_account", parent, update_modified=False)

	if repairs or any(not account.lft or account.rgt <= account.lft for account in accounts):
		rebuild_tree("Account", "parent_account")

	_verify_samif_tree(len(accounts), roots)


def _verify_samif_tree(expected_count, roots):
	accounts = frappe.get_all(
		"Account",
		filters={"company": COMPANY},
		fields=["name", "account_number", "parent_account", "root_type", "is_group", "lft", "rgt"],
		limit_page_length=0,
	)
	if len(accounts) != expected_count:
		frappe.throw("Samif Account count changed during the hierarchy repair.")

	by_name = {account.name: account for account in accounts}
	actual_roots = {account.name for account in accounts if not account.parent_account}
	if roots is None:
		if not actual_roots or any(
			not account.is_group or not account.account_number
			for account in accounts if not account.parent_account
		):
			frappe.throw("Samif SAP chart roots are missing or invalid.")
	elif actual_roots != set(roots.values()):
		frappe.throw("Samif Account roots do not match the five company roots.")

	for account in accounts:
		if not account.lft or account.rgt <= account.lft:
			frappe.throw(f"Samif Account has invalid tree bounds: {account.name}")
		if not account.parent_account:
			continue
		parent = by_name.get(account.parent_account)
		if (
			not parent or not parent.is_group or parent.root_type != account.root_type
			or not (parent.lft < account.lft < account.rgt < parent.rgt)
		):
			frappe.throw(f"Samif Account has invalid parent: {account.name}")
