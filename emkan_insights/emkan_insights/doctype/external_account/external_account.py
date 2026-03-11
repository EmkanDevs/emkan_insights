# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import json
from typing import List, Union

import frappe
from frappe.model.document import Document
from frappe.utils import cint


SYSTEM_FIELDS = {
    "name",
    "owner",
    "creation",
    "modified",
    "modified_by",
    "docstatus",
    "idx",
    "__last_sync_on",
}


class ExternalAccount(Document):
    # def before_naming(self):
    #     self.set_naming_series()

    # def before_validate(self):
    #     self.set_naming_series()

    # def validate(self):
    #     self.set_naming_series()

    def set_naming_series(self):
        company_abbr = frappe.db.get_value("Company", self.company, "abbr") if self.company else None

        parts = [
            (self.account_number or "").strip(),
            (self.account_name or "").strip(),
            (company_abbr or "").strip(),
        ]

        self.naming_series = "-".join(part for part in parts if part)


@frappe.whitelist()
def add_external_account(args=None):
    from frappe.desk.treeview import make_tree_args

    if not args:
        args = frappe.local.form_dict

    args.doctype = "External Account"
    args = make_tree_args(**args)
    generated_parent_field = "parent_" + frappe.scrub(args.doctype)
    if generated_parent_field in args and generated_parent_field != "parent_account":
        args.pop(generated_parent_field)

    external_account = frappe.new_doc("External Account")

    if args.get("ignore_permissions"):
        external_account.flags.ignore_permissions = True
        args.pop("ignore_permissions")

    external_account.update(args)

    if not external_account.parent_account:
        external_account.parent_account = args.get("parent")

    external_account.old_parent = ""
    if cint(external_account.get("is_root")):
        external_account.parent_account = None
        external_account.flags.ignore_mandatory = True

    external_account.insert()
    return external_account.name


@frappe.whitelist()
def get_external_account_children(doctype, parent, company, is_root=False):
    parent_fieldname = frappe.get_meta("External Account").get("nsm_parent_field") or "parent_account"
    fields = ["name as value", "account_name as title", "is_group as expandable"]
    filters = [["docstatus", "<", 2]]

    filters.append([f'ifnull(`{parent_fieldname}`,"")', "=", "" if is_root else parent])

    if is_root:
        filters.append(["company", "=", company])
    else:
        fields += [parent_fieldname + " as parent"]

    return frappe.get_list(
        "External Account",
        fields=fields,
        filters=filters,
        order_by="lft asc, name asc",
    )


@frappe.whitelist()
def sync_external_accounts(external_accounts: Union[str, List[str]]):
    if isinstance(external_accounts, str):
        external_accounts = json.loads(external_accounts)

    results = []
    stack = []
    for external_name in external_accounts:
        results.append(_sync_one_external_account(external_name, stack))

    return results


@frappe.whitelist()
def sync_external_accounts_for_company(external_accounts: Union[str, List[str]], company: str):
    if isinstance(external_accounts, str):
        external_accounts = json.loads(external_accounts)

    if not company:
        frappe.throw("Company is required")

    results = []
    stack = []
    for external_name in external_accounts:
        results.append(_sync_one_external_account(external_name, stack, company=company))

    return results


@frappe.whitelist()
def sync_external_records(names: Union[str, List[str]]):
    return sync_external_accounts(names)


def _sync_one_external_account(external_name: str, stack: list = None, company: str | None = None) -> str:
    if stack is None:
        stack = []

    if external_name in stack:
        return _find_account_name_by_external_name(external_name, target_company=company) or external_name

    stack.append(external_name)
    try:
        src = frappe.get_doc("External Account", external_name)
        tgt_name = _find_existing_target_account(src, target_company=company)

        if tgt_name:
            tgt = frappe.get_doc("Account", tgt_name)
        else:
            tgt = frappe.new_doc("Account")

        _map_fields(src, tgt)
        if company:
            tgt.company = company
        _apply_parent_rules(src, tgt, stack, target_company=company)
        _apply_root_defaults(src, tgt)
        _apply_remote_id(src, tgt)

        tgt.flags.ignore_root_company_validation = True
        tgt.flags.ignore_mandatory = False
        tgt.save(ignore_permissions=True)
        return tgt.name
    finally:
        if external_name in stack:
            stack.remove(external_name)


def _find_existing_target_account(src, target_company: str | None = None) -> str | None:
    key_field = _account_key_field()
    remote_id = src.get("remote_id")
    company = target_company or src.get("company")

    if key_field and remote_id:
        filters = {key_field: remote_id}
        if company:
            filters["company"] = company
        name = frappe.db.get_value("Account", filters, "name")
        if name:
            return name

    if src.get("account_number") and company:
        name = frappe.db.get_value(
            "Account",
            {"account_number": src.account_number, "company": company},
            "name",
        )
        if name:
            return name

    if src.get("account_name") and company:
        return frappe.db.get_value(
            "Account",
            {"account_name": src.account_name, "company": company},
            "name",
        )

    return None


def _map_fields(src, tgt) -> None:
    target_fields = {df.fieldname for df in frappe.get_meta("Account").fields}
    ignore = SYSTEM_FIELDS | {"parent_account"}

    for field, value in src.as_dict().items():
        if field in ignore:
            continue

        target_field = field
        if field not in target_fields and f"custom_{field}" in target_fields:
            target_field = f"custom_{field}"

        if target_field in target_fields:
            tgt.set(target_field, value)


def _apply_parent_rules(src, tgt, stack: list = None, target_company: str | None = None) -> None:
    if stack is None:
        stack = []

    parent_external = src.get("parent_account")

    if not parent_external:
        tgt.parent_account = None
        return

    parent_name = _resolve_or_sync_parent_account(parent_external, stack, target_company=target_company)
    if not parent_name:
        frappe.throw(
            f"Unable to resolve parent Account for External Account {src.name}. "
            f"Parent External Account: {parent_external}"
        )

    tgt.parent_account = parent_name

    if not tgt.root_type:
        tgt.root_type = frappe.db.get_value("Account", parent_name, "root_type")
    if not tgt.report_type:
        tgt.report_type = frappe.db.get_value("Account", parent_name, "report_type")


def _apply_root_defaults(src, tgt) -> None:
    if tgt.get("parent_account"):
        return

    if not tgt.get("is_group"):
        tgt.is_group = 1

    if not tgt.get("root_type"):
        tgt.root_type = src.get("root_type")

    if not tgt.get("report_type") and tgt.get("root_type"):
        tgt.report_type = (
            "Balance Sheet"
            if tgt.root_type in ("Asset", "Liability", "Equity")
            else "Profit and Loss"
        )


def _apply_remote_id(src, tgt) -> None:
    key_field = _account_key_field()
    if key_field and src.get("remote_id"):
        tgt.set(key_field, src.remote_id)


def _account_key_field() -> str | None:
    """Get the actual field name for remote_id in Account doctype."""
    meta_fields = {df.fieldname for df in frappe.get_meta("Account").fields}
    if "remote_id" in meta_fields:
        return "remote_id"
    if "custom_remote_id" in meta_fields:
        return "custom_remote_id"
    if frappe.db.has_column("Account", "custom_remote_id"):
        return "custom_remote_id"
    return None


def _resolve_or_sync_parent_account(parent_external_name: str, stack: list = None, target_company: str | None = None) -> str | None:
    """Resolve parent account, syncing it first if needed."""
    if stack is None:
        stack = []

    # Check if parent already exists as synced Account
    existing = _find_account_name_by_external_name(parent_external_name, target_company=target_company)
    if existing:
        return existing

    # Check if parent_external_name is actually an Account name directly
    if frappe.db.exists("Account", parent_external_name):
        return parent_external_name

    # If parent External Account doesn't exist, we can't sync it
    if not frappe.db.exists("External Account", parent_external_name):
        return None

    # Check for circular reference
    if parent_external_name in stack:
        frappe.throw(
            f"Circular reference detected: {parent_external_name} is already being synced. "
            f"Sync stack: {' -> '.join(stack + [parent_external_name])}"
        )

    # Sync the parent External Account recursively
    _sync_one_external_account(parent_external_name, stack, company=target_company)

    # Return the synced account name
    return _find_account_name_by_external_name(parent_external_name, target_company=target_company)


def _find_account_name_by_external_name(external_name: str, target_company: str | None = None) -> str | None:
    """Find the Account name that corresponds to an External Account."""
    if not frappe.db.exists("External Account", external_name):
        return None

    ext_remote_id = frappe.db.get_value("External Account", external_name, "remote_id")
    if not ext_remote_id:
        return None

    key_field = _account_key_field()
    if key_field:
        filters = {key_field: ext_remote_id}
        if target_company:
            filters["company"] = target_company
        name = frappe.db.get_value("Account", filters, "name")
        if name:
            return name

    # Fallback by account identity fields
    details = frappe.db.get_value(
        "External Account",
        external_name,
        ["account_number", "account_name", "company"],
        as_dict=True,
    )
    company = target_company or (details.company if details else None)
    if details and details.get("account_number") and company:
        name = frappe.db.get_value(
            "Account",
            {"account_number": details.account_number, "company": company},
            "name",
        )
        if name:
            return name

    if details and details.get("account_name") and company:
        return frappe.db.get_value(
            "Account",
            {"account_name": details.account_name, "company": company},
            "name",
        )

    return None
