"""Read-only review of historical SAP Journal Entry account links."""

from collections import defaultdict
from decimal import Decimal

import frappe

from emkan_insights.sap.services.client import SAPB1Client


ACCOUNT_FIELDS = (
	"Code", "Name", "FormatCode", "FatherAccountKey", "AccountType",
	"AcctCurrency", "ActiveAccount",
)


def fetch_unresolved_sap_accounts(configuration_name: str) -> dict:
	"""Read SAP classification fields for account codes missing in ERPNext."""
	audit = audit_journal_accounts(configuration_name)
	configuration = frappe.get_doc("External SAP Configuration", configuration_name)
	client = _sap_client(configuration)
	records = []
	for item in audit["unresolved_codes"]:
		code = item["code"]
		if not code or not code.isalnum():
			raise frappe.ValidationError("An unresolved account code cannot be used in a SAP filter.")
		matches = _sap_account_matches(client, code)
		records.append({
			"code": code,
			"journal_lines": item["lines"],
			"matches": matches,
		})
	return {"company": audit["company"], "records": records}


def fetch_unresolved_account_hierarchy(configuration_name: str) -> dict:
	"""Read each missing account and its SAP parent chain."""
	audit = audit_journal_accounts(configuration_name)
	configuration = frappe.get_doc("External SAP Configuration", configuration_name)
	client = _sap_client(configuration)
	accounts = {}
	for item in audit["unresolved_codes"]:
		code = item["code"]
		chain = set()
		while code and code not in accounts:
			if code in chain or len(chain) >= 30:
				raise frappe.ValidationError("SAP Account hierarchy contains a cycle or is too deep.")
			chain.add(code)
			matches = _sap_account_matches(client, code)
			if len(matches) != 1:
				raise frappe.ValidationError(f"SAP Account {code} must resolve to one source record.")
			accounts[code] = matches[0]
			code = accounts[code]["FatherAccountKey"]
	return {
		"company": audit["company"],
		"missing_codes": [item["code"] for item in audit["unresolved_codes"]],
		"accounts": [accounts[code] for code in sorted(accounts)],
	}


def audit_journal_accounts(configuration_name: str) -> dict:
	configuration = frappe.get_doc("External SAP Configuration", configuration_name)
	company = configuration.company
	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_number"],
		limit_page_length=0,
	)
	names = {account.name for account in accounts}
	numbers = defaultdict(list)
	for account in accounts:
		if account.account_number:
			numbers[str(account.account_number)].append(account.name)

	journals = frappe.get_all(
		"Journal Entry",
		filters={"company": company},
		pluck="name",
		limit_page_length=0,
	)
	counts = defaultdict(int)
	unknown = defaultdict(lambda: {
		"lines": 0, "debit": Decimal(0), "credit": Decimal(0),
		"account_debit": Decimal(0), "account_credit": Decimal(0), "journals": set(),
	})
	for journal_batch in _chunks(journals, 500):
		lines = frappe.get_all(
			"Journal Entry Account",
			filters={"parent": ["in", journal_batch], "parenttype": "Journal Entry"},
			fields=[
				"parent", "account", "debit", "credit",
				"debit_in_account_currency", "credit_in_account_currency",
			],
			limit_page_length=0,
		)
		for line in lines:
			code = str(line.account or "").strip()
			local_debit = Decimal(str(line.debit or 0))
			local_credit = Decimal(str(line.credit or 0))
			account_debit = Decimal(str(line.debit_in_account_currency or 0))
			account_credit = Decimal(str(line.credit_in_account_currency or 0))
			if not (local_debit or local_credit) and (account_debit or account_credit):
				counts["zero_local_with_account_currency_amount"] += 1
			if code in names:
				counts["valid_links"] += 1
			elif len(numbers.get(code, [])) == 1:
				counts["resolvable_codes"] += 1
			else:
				counts["unresolved_lines"] += 1
				entry = unknown[code]
				entry["lines"] += 1
				entry["debit"] += local_debit
				entry["credit"] += local_credit
				entry["account_debit"] += account_debit
				entry["account_credit"] += account_credit
				entry["journals"].add(line.parent)

	staged_ids = set(frappe.get_all(
		"External Account",
		filters={"external_sap_configuration": configuration_name, "remote_id": ["in", list(unknown)]},
		pluck="remote_id",
		limit_page_length=0,
	)) if unknown else set()
	return {
		"company": company,
		"journal_entries": len(journals),
		"valid_links": counts["valid_links"],
		"resolvable_codes": counts["resolvable_codes"],
		"unresolved_lines": counts["unresolved_lines"],
		"zero_local_with_account_currency_amount": counts["zero_local_with_account_currency_amount"],
		"unresolved_codes": [{
			"code": code,
			"lines": entry["lines"],
			"journal_count": len(entry["journals"]),
			"debit": str(entry["debit"]),
			"credit": str(entry["credit"]),
			"debit_in_account_currency": str(entry["account_debit"]),
			"credit_in_account_currency": str(entry["account_credit"]),
			"reason": "ambiguous_account_number" if numbers.get(code) else "missing_account_number",
			"staged_account": code in staged_ids,
		} for code, entry in sorted(unknown.items())],
	}


def _chunks(values, size):
	for offset in range(0, len(values), size):
		yield values[offset:offset + size]


def _sap_client(configuration) -> SAPB1Client:
	return SAPB1Client(
		configuration.base_url,
		configuration.company_db,
		configuration.username,
		configuration.get_password("password"),
		session_timeout_minutes=configuration.session_timeout_minutes or 25,
		session_id=configuration.get_password("session_id", raise_exception=False),
	)


def _sap_account_matches(client: SAPB1Client, code: str) -> list[dict]:
	if not code or not code.isalnum():
		raise frappe.ValidationError("A SAP Account code cannot be used in a filter.")
	response = client.get("ChartOfAccounts", params={"$filter": f"Code eq '{code}'", "$top": "2"})
	matches = response.get("value")
	if not isinstance(matches, list):
		raise frappe.ValidationError("SAP ChartOfAccounts response has no value array.")
	return [{field: row.get(field) for field in ACCOUNT_FIELDS} for row in matches]
