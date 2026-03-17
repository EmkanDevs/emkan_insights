import frappe
import json

@frappe.whitelist()
def sync_account_docs(source_doctype: str, names, company: str):

    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        sync_external_account(name, company)

    return "Success"


def sync_external_account(external_name, company):

    external = frappe.get_doc("External Account", external_name)

    # Check if already created in Account
    existing = frappe.get_value(
        "Account",
        {
            "account_number": external.account_number,
            "company": company
        },
        "name"
    )
    if existing:
        return existing

    # 🔁 Resolve Parent from External
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

    account.insert(ignore_permissions=True)

    return account.name


    root = frappe.get_value(
        "Account",
        {
            "company": company,
            "root_type": root_type,
            "parent_account": ["is", "not set"]
        },
        "name"
    )

    if root:
        return root

    root_doc = frappe.get_doc({
        "doctype": "Account",
        "account_name": root_type,
        "company": company,
        "root_type": root_type,
        "is_group": 1,
        "report_type": "Balance Sheet" if root_type in ["Asset", "Liability", "Equity"] else "Profit and Loss"
    })

    root_doc.insert(ignore_permissions=True)

    return root_doc.name