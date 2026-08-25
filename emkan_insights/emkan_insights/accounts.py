# import frappe
# import json

# @frappe.whitelist()
# def sync_account_docs(source_doctype: str, names, company: str):

#     if isinstance(names, str):
#         names = json.loads(names)

#     for name in names:
#         sync_external_account(name, company)

#     return "Success"


# def sync_external_account(external_name, company):

#     external = frappe.get_doc("External Account", external_name)

#     # Check if already created in Account
#     existing = frappe.get_value(
#         "Account",
#         {
#             "account_number": external.account_number,
#             "company": company
#         },
#         "name"
#     )
#     if existing:
#         return existing

#     # 🔁 Resolve Parent from External
#     external_parent = external.parent_account
#     parent_account = None

#     if external_parent:
#         parent_account = sync_external_account(external_parent, company)
   
#     account = frappe.get_doc({
#         "doctype": "Account",
#         "account_name": external.account_name,
#         "account_number": external.account_number,
#         "account_type": external.account_type,
#         "company": company,
#         "parent_account": parent_account,
#         "is_group": external.is_group,
#         "root_type": external.root_type,
#         "report_type": external.report_type,
#         "account_currency": external.account_currency
#     })

    
#     account.flags.ignore_root_company_validation = True
#     account.flags.ignore_mandatory = True
#     account.flags.ignore_links = True
#     account.insert(ignore_permissions=True)
#     return account.name


#     root = frappe.get_value(
#         "Account",
#         {
#             "company": company,
#             "root_type": root_type,
#             "parent_account": ["is", "not set"]
#         },
#         "name"
#     )

#     if root:
#         return root

#     root_doc = frappe.get_doc({
#         "doctype": "Account",
#         "account_name": root_type,
#         "company": company,
#         "root_type": root_type,
#         "is_group": 1,
#         "report_type": "Balance Sheet" if root_type in ["Asset", "Liability", "Equity"] else "Profit and Loss"
#     })

#     root_doc.insert(ignore_permissions=True)

#     return root_doc.name


import frappe
import json


@frappe.whitelist()
def sync_account_docs(source_doctype: str, names, company: str):

    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        try:
            external = frappe.get_doc(source_doctype, name)
            sync_started_at = external.modified

            result = sync_external_account(name, company)

            external.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Account Sync Failed: {name}")

    return "Success"


def _find_existing_account(candidate_name, company):
    """Return candidate_name if an Account with that name already belongs
    to this company, else None. Since Account.name is a global primary
    key (not scoped per company), a name match alone isn't enough - it
    could be a different company's account."""
    if frappe.db.exists("Account", candidate_name):
        if frappe.db.get_value("Account", candidate_name, "company") == company:
            return candidate_name
    return None


def _find_existing_account_by_number(account_number, company):
    if not account_number or not company:
        return None

    return frappe.db.get_value(
        "Account",
        {"account_number": account_number, "company": company},
        "name",
    )


def sync_external_account(external_name, company):

    external = frappe.get_doc("External Account", external_name)

    # REQUIREMENT: the id synced onto Account must be the remote_id from
    # the External Account doc. Fall back to the External Account's own
    # document name if it doesn't have a remote_id field, so this still
    # works before that field is added.
    remote_id = external.get("remote_id") or external.name
    company_abbr = frappe.db.get_value("Company", company, "abbr") or ""

    # Check if already synced under either the raw remote_id or the
    # company-prefixed variant (see collision handling below).
    existing = _find_existing_account(remote_id, company)
    if not existing and company_abbr:
        existing = _find_existing_account(f"{company_abbr}-{remote_id}", company)
    if not existing:
        existing = _find_existing_account_by_number(external.account_number, company)
    if existing:
        return existing

    # Prefer the raw remote_id as the new Account's name. Only prefix
    # with the company abbreviation if that raw name is already taken -
    # by construction that means it belongs to a DIFFERENT company
    # (the same-company case was already returned above), so we need a
    # distinct name to avoid a global name collision.
    target_name = remote_id
    if frappe.db.exists("Account", target_name):
        target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

    # Resolve Parent from External
    external_parent = external.parent_account
    parent_account = None

    if external_parent:
        parent_account = sync_external_account(external_parent, company)

    account = frappe.get_doc({
        "doctype": "Account",
        "account_name": external.account_name,
        "account_number": external.account_number,
        "account_type": external.account_type,
        "company": company,
        "parent_account": parent_account,
        "is_group": external.is_group,
        "root_type": external.root_type,
        "report_type": external.report_type,
        "account_currency": external.account_currency
    })

    # FIX: force the Account's name to the External Account's remote_id
    # instead of letting ERPNext's autoname() derive it from
    # account_name/account_number + company abbr. flags.name_set = True
    # tells Frappe's naming logic to respect the name we've already set,
    # the same mechanism used for Journal Entry in the JE sync.
    account.name = target_name
    account.flags.name_set = True

    account.flags.ignore_root_company_validation = True
    account.flags.ignore_mandatory = True
    account.flags.ignore_links = True
    account.insert(ignore_permissions=True)

    # Also persist remote_id as a field, if Account has one, so the id is
    # recoverable even if the name is ever changed/renamed later.
    if frappe.get_meta("Account").has_field("remote_id"):
        frappe.db.set_value(
            "Account", account.name, "remote_id", remote_id, update_modified=False
        )

    return account.name
