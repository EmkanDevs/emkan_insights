import re

import frappe


def _account_company(account):
    if not account:
        return None
    return frappe.db.get_value("Account", account, "company")


def _extract_account_number(account):
    if not account:
        return None

    account = account.strip()
    match = re.search(r"(\d{4,})", account)
    return match.group(1) if match else None


def resolve_company_account(account, company, company_abbr=None):
    """
    Resolve a remote account reference to a local Account for the target company.

    Account.name is global, while remote names can collide across companies.
    Never return an Account unless its Account.company exactly matches company.
    """
    if not account or not company:
        return None

    account = account.strip()
    company_abbr = company_abbr or frappe.db.get_value("Company", company, "abbr") or ""

    candidates = []
    if company_abbr and not account.startswith(f"{company_abbr}-"):
        candidates.append(f"{company_abbr}-{account}")
    candidates.extend((account, f"{account}-{company_abbr}", f"{account} - {company_abbr}"))

    for candidate in candidates:
        if _account_company(candidate) == company:
            return candidate

    by_account_name = frappe.db.get_value(
        "Account",
        {"account_name": account, "company": company},
        "name",
    )
    if by_account_name:
        return by_account_name

    account_number = _extract_account_number(account)
    if account_number:
        by_number = frappe.db.get_value(
            "Account",
            {"account_number": account_number, "company": company},
            "name",
        )
        if by_number:
            return by_number

        by_name = frappe.db.sql(
            """
            SELECT name
            FROM `tabAccount`
            WHERE company = %s AND name LIKE %s
            ORDER BY
                CASE WHEN name LIKE %s THEN 0 ELSE 1 END,
                name
            LIMIT 1
            """,
            (
                company,
                f"%{account_number}%",
                f"{company_abbr}-{account_number}%" if company_abbr else f"{account_number}%",
            ),
        )
        if by_name:
            return by_name[0][0]

    return None
