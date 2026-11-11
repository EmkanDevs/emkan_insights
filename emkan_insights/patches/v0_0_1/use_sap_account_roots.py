"""Use SAP chart groups as Account roots for configured SAP companies."""

import json
from pathlib import Path

import frappe
from frappe.utils.nestedset import rebuild_tree


ROOT_TYPES = ("Asset", "Liability", "Equity", "Income", "Expense")
EQUITY_LEDGER_CODES = frozenset({"911400", "profit"})
EQUITY_GROUP_CODE = "300000000000000"


def execute():
	companies = frappe.get_all("External SAP Configuration", pluck="company")
	if not companies:
		return

	for company in sorted(set(companies)):
		_reparent_company_accounts(company)
	_update_account_mapping()
	rebuild_tree("Account", "parent_account")
	_validate_account_trees(companies)


def _reparent_company_accounts(company):
	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_number", "parent_account", "root_type", "is_group"],
		limit_page_length=0,
	)
	if not accounts:
		return

	by_name = {account.name: account for account in accounts}
	abbr = frappe.db.get_value("Company", company, "abbr")
	if not abbr:
		frappe.throw(f"Company {company} has no abbreviation.")
	roots = {root_type: f"{root_type} - {abbr}" for root_type in ROOT_TYPES}
	if all(name not in by_name for name in roots.values()):
		return
	for root_type, name in roots.items():
		root = by_name.get(name)
		if not root or root.parent_account or root.account_number or not root.is_group or root.root_type != root_type:
			frappe.throw(f"Invalid ERPNext Account root for {company}: {name}")

	equity_groups = [account for account in accounts if account.account_number == EQUITY_GROUP_CODE]
	if len(equity_groups) != 1 or not equity_groups[0].is_group:
		frappe.throw(f"SAP equity group {EQUITY_GROUP_CODE} is missing for {company}.")
	equity_group = equity_groups[0].name

	changes = {}
	for account in accounts:
		if account.parent_account != roots.get(account.root_type):
			continue
		if account.account_number in EQUITY_LEDGER_CODES:
			if account.root_type != "Equity":
				frappe.throw(f"Unexpected root type for SAP Account {account.name}.")
			changes[account.name] = equity_group
		elif account.account_number and account.is_group:
			changes[account.name] = None
		else:
			frappe.throw(f"Cannot make SAP Account {account.name} a root in {company}.")

	for name, parent in changes.items():
		frappe.db.set_value("Account", name, "parent_account", parent, update_modified=False)
	for name in roots.values():
		frappe.delete_doc("Account", name, ignore_permissions=True)


def _update_account_mapping():
	if not frappe.db.exists("SAP Doctype Mapping", "SAP Account"):
		return
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
	doc = frappe.get_doc("SAP Doctype Mapping", "SAP Account")
	mapping = frappe.parse_json(doc.field_mapping)
	mapping["parent_account"] = catalog["SAP Account"]["field_mapping"]["parent_account"]
	doc.field_mapping = json.dumps(mapping, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)


def _validate_account_trees(companies):
	for company in sorted(set(companies)):
		accounts = frappe.get_all(
			"Account", filters={"company": company},
			fields=["name", "parent_account", "root_type", "is_group", "lft", "rgt"],
			limit_page_length=0,
		)
		by_name = {account.name: account for account in accounts}
		for account in accounts:
			if not account.lft or not account.rgt or account.lft >= account.rgt:
				frappe.throw(f"Invalid Account tree interval: {account.name}")
			if not account.parent_account:
				if not account.is_group:
					frappe.throw(f"Ledger Account cannot be a root: {account.name}")
				continue
			parent = by_name.get(account.parent_account)
			if not parent or not parent.is_group:
				frappe.throw(f"Invalid Account parent: {account.name}")
			if not parent.lft < account.lft < account.rgt < parent.rgt:
				frappe.throw(f"Invalid Account tree interval: {account.name}")
