"""Check ERPNext links before the SAP bulk writer bypasses document validation."""

from collections import defaultdict

import frappe


def resolve_account_links(documents: list[dict[str, list[dict]]]) -> None:
	"""Replace SAP account numbers with ERP Account names in a prepared batch."""
	batch_accounts = {}
	for tree in documents:
		for account in tree.get("Account", []):
			if account.get("company") and account.get("account_number"):
				batch_accounts[(account["company"], str(account["account_number"]))] = account["name"]

	resolved = {}
	# A batch repeats the same accounts on every row, so ask the database once per value.
	account_fields = {}
	exists = {}

	def account_exists(value) -> bool:
		if value not in exists:
			exists[value] = bool(frappe.db.exists("Account", value))
		return exists[value]

	for tree in documents:
		company = next((row.get("company") for rows in tree.values() for row in rows if row.get("company")), None)
		for doctype, rows in tree.items():
			if doctype not in account_fields:
				account_fields[doctype] = [
					field.fieldname for field in frappe.get_meta(doctype).fields
					if field.fieldtype == "Link" and field.options == "Account"
				]
			for row in rows:
				for fieldname in account_fields[doctype]:
					value = row.get(fieldname)
					if value in (None, "") or account_exists(value):
						continue
					account_company = row.get("company") or row.get("company_name") or company
					key = (account_company, str(value))
					if key not in resolved:
						matches = frappe.get_all(
							"Account",
							filters={"company": account_company, "account_number": str(value)},
							pluck="name",
							limit_page_length=2,
						) if account_company else []
						resolved[key] = matches[0] if len(matches) == 1 else (
							batch_accounts.get(key) if not matches else None
						)
					if resolved[key]:
						row[fieldname] = resolved[key]


def validate_document_links(documents: list[dict[str, list[dict]]]) -> None:
	"""Reject a batch if any Link points outside ERPNext and this batch."""
	values_by_doctype = defaultdict(set)
	batch_names = defaultdict(set)
	for tree in documents:
		for doctype, rows in tree.items():
			batch_names[doctype].update(str(row["name"]) for row in rows if row.get("name"))
			for row in rows:
				for field in frappe.get_meta(doctype).fields:
					if field.fieldtype != "Link" or not field.options:
						continue
					value = row.get(field.fieldname)
					if value not in (None, ""):
						values_by_doctype[field.options].add(str(value))

	for doctype, values in values_by_doctype.items():
		missing = values - batch_names[doctype]
		if not missing:
			continue
		found = set()
		for names in _chunks(sorted(missing), 500):
			found.update(frappe.get_all(
				doctype,
				filters={"name": ["in", names]},
				pluck="name",
				limit_page_length=0,
			))
		missing -= found
		if missing:
			preview = ", ".join(sorted(missing)[:5])
			raise frappe.ValidationError(
				f"SAP sync cannot resolve {len(missing)} {doctype} link(s): {preview}"
			)


def _chunks(values: list[str], size: int):
	for offset in range(0, len(values), size):
		yield values[offset:offset + size]
