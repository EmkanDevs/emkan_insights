"""Read-only SAP and ERPNext ledger comparison at a posting date."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from hashlib import sha256

import frappe
from frappe.utils import getdate

from emkan_insights.sap.services.client import SAPB1Client


BALANCE_SHEET_ROOTS = {"Asset", "Liability", "Equity"}


def compare_ledgers(configuration_name: str, as_of_date: str) -> dict:
	"""Compare all SAP journal lines with posted ERP GL by account number."""
	as_of = getdate(as_of_date).isoformat()
	configuration = frappe.get_doc("External SAP Configuration", configuration_name)
	company = configuration.company
	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_number", "root_type"],
		limit_page_length=0,
	)
	by_number = _account_index(accounts)
	client = _sap_client(configuration)
	sap, sap_lines, journal_ids = _sap_balances(client, as_of)
	header_ids, _ = _sap_journal_ids(client, as_of)
	if journal_ids != header_ids:
		raise frappe.ValidationError("SAP journal header and detail pages returned different journal IDs.")
	erp = _erp_balances(company, as_of)
	return _comparison(company, as_of, by_number, sap, erp, sap_lines, journal_ids, source_verified=True)


def verify_journal_pagination(configuration_name: str, as_of_date: str) -> dict:
	"""Traverse SAP journal headers independently of their line payloads."""
	as_of = getdate(as_of_date).isoformat()
	configuration = frappe.get_doc("External SAP Configuration", configuration_name)
	client = _sap_client(configuration)
	journal_ids, pages = _sap_journal_ids(client, as_of)
	return {
		"journal_entries": len(journal_ids),
		"pages": pages,
		"journal_id_sha256": _journal_id_digest(journal_ids),
	}


def _sap_journal_ids(client: SAPB1Client, as_of: str) -> tuple[set, int]:
	endpoint = "JournalEntries"
	params = {"$filter": f"ReferenceDate le '{as_of}'", "$select": "JdtNum"}
	journal_ids = set()
	pages = 0
	while endpoint:
		response = client.get(endpoint, params=params)
		entries = response.get("value")
		if not isinstance(entries, list):
			raise frappe.ValidationError("SAP JournalEntries headers have no value array.")
		for entry in entries:
			journal_id = entry.get("JdtNum")
			if journal_id is None or journal_id in journal_ids:
				raise frappe.ValidationError("SAP JournalEntries headers have a missing or duplicate ID.")
			journal_ids.add(journal_id)
		pages += 1
		endpoint = response.get("@odata.nextLink") or response.get("odata.nextLink")
		params = None
	return journal_ids, pages


def _account_index(accounts) -> dict[str, dict]:
	by_number = {}
	for account in accounts:
		number = str(account.account_number or "").strip()
		if not number:
			continue
		if number in by_number:
			raise frappe.ValidationError(f"Duplicate ERP Account number {number} in the company.")
		by_number[number] = account
	return by_number


def _sap_balances(client: SAPB1Client, as_of: str) -> tuple[dict, int, set]:
	balances = defaultdict(lambda: [Decimal(0), Decimal(0)])
	endpoint = "JournalEntries"
	params = {"$filter": f"ReferenceDate le '{as_of}'"}
	line_count = 0
	journal_ids = set()
	while endpoint:
		page = client.get(endpoint, params=params)
		entries = page.get("value")
		if not isinstance(entries, list):
			raise frappe.ValidationError("SAP JournalEntries response has no value array.")
		for entry in entries:
			journal_id = entry.get("JdtNum")
			if journal_id is None or journal_id in journal_ids:
				raise frappe.ValidationError("SAP journal entry has a missing or duplicate ID.")
			journal_ids.add(journal_id)
			lines = entry.get("JournalEntryLines")
			if not isinstance(lines, list):
				raise frappe.ValidationError("SAP journal entry has no JournalEntryLines array.")
			for line in lines:
				account = str(line.get("AccountCode") or "").strip()
				if not account:
					raise frappe.ValidationError("SAP journal line has no AccountCode.")
				balances[account][0] += _money(line.get("Debit"), "SAP Debit")
				balances[account][1] += _money(line.get("Credit"), "SAP Credit")
				line_count += 1
		endpoint = page.get("@odata.nextLink") or page.get("odata.nextLink")
		params = None
	return balances, line_count, journal_ids


def _erp_balances(company: str, as_of: str) -> dict:
	rows = frappe.get_all(
		"GL Entry",
		filters={"company": company, "posting_date": ["<=", as_of], "is_cancelled": 0},
		fields=["account", "sum(debit) as debit", "sum(credit) as credit"],
		group_by="account",
		limit_page_length=0,
	)
	return {
		row.account: [_money(row.debit, "ERP debit"), _money(row.credit, "ERP credit")]
		for row in rows
	}


def _comparison(company, as_of, by_number, sap, erp, sap_lines, journal_ids=None, source_verified=False) -> dict:
	name_to_number = {account.name: number for number, account in by_number.items()}
	missing_sap_accounts = sorted(number for number in sap if number not in by_number)
	missing_erp_accounts = sorted(name for name in erp if name not in name_to_number)
	unmatched_sap_balances = [{
		"account_number": number,
		"debit": str(sap[number][0]),
		"credit": str(sap[number][1]),
		"balance": str(sap[number][0] - sap[number][1]),
	} for number in missing_sap_accounts]
	rows = []
	for number in sorted(set(sap) | {name_to_number[name] for name in erp if name in name_to_number}):
		account = by_number.get(number)
		if not account:
			continue
		sap_debit, sap_credit = sap.get(number, (Decimal(0), Decimal(0)))
		erp_debit, erp_credit = erp.get(account.name, (Decimal(0), Decimal(0)))
		difference = sap_debit - sap_credit - erp_debit + erp_credit
		if difference:
			rows.append({
				"account": account.name,
				"account_number": number,
				"root_type": account.root_type,
				"sap_balance": str(sap_debit - sap_credit),
				"erp_balance": str(erp_debit - erp_credit),
				"difference": str(difference),
			})
	bs_rows = [row for row in rows if row["root_type"] in BALANCE_SHEET_ROOTS]
	sap_total = sum((debit - credit for debit, credit in sap.values()), Decimal(0))
	erp_total = sum((debit - credit for debit, credit in erp.values()), Decimal(0))
	return {
		"company": company,
		"as_of_date": as_of,
		"sap_journal_lines": sap_lines,
		"sap_journal_entries": len(journal_ids or []),
		"sap_journal_id_sha256": _journal_id_digest(journal_ids or set()),
		"sap_source_verified": source_verified,
		"erp_gl_accounts": len(erp),
		"sap_net_total": str(sap_total),
		"erp_net_total": str(erp_total),
		"net_difference": str(sap_total - erp_total),
		"unmatched_sap_account_numbers": missing_sap_accounts,
		"unmatched_sap_account_balances": unmatched_sap_balances,
		"unmatched_erp_account_names": missing_erp_accounts,
		"balance_sheet_account_differences": bs_rows,
		"other_account_differences": [row for row in rows if row["root_type"] not in BALANCE_SHEET_ROOTS],
		"matched": source_verified and sap_lines > 0 and not (missing_sap_accounts or missing_erp_accounts or rows),
	}


def _money(value, label: str) -> Decimal:
	try:
		amount = Decimal(str(value if value is not None else 0))
	except (InvalidOperation, ValueError) as exc:
		raise frappe.ValidationError(f"{label} is not numeric.") from exc
	if not amount.is_finite():
		raise frappe.ValidationError(f"{label} is not finite.")
	return amount


def _journal_id_digest(journal_ids: set) -> str:
	return sha256("\n".join(str(value) for value in sorted(journal_ids)).encode()).hexdigest()


def _sap_client(configuration) -> SAPB1Client:
	return SAPB1Client(
		configuration.base_url,
		configuration.company_db,
		configuration.username,
		configuration.get_password("password"),
		session_timeout_minutes=configuration.session_timeout_minutes or 25,
		session_id=configuration.get_password("session_id", raise_exception=False),
	)
