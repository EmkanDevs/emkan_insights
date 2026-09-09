import json

import frappe

from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.tree_sync import (
    order_tree_sync_names,
    resolve_tree_source_name,
)


@frappe.whitelist()
@filter_kwargs
def sync_cost_center_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)
    if not names:
        names = frappe.db.sql("""
            SELECT name
            FROM `tabExternal Cost Center`
            WHERE
                last_synced_on IS NULL
                OR modified >= last_synced_on
        """, pluck=True)

    names = order_tree_sync_names(
        "External Cost Center",
        names,
        "parent_cost_center",
    )
    created = []

    for name in names:
        cc = create_cost_center_from_external(name)
        created.append(cc)

    return created


def create_cost_center_from_external(external_name, sync_stack=None):

    sync_stack = list(sync_stack or [])
    if external_name in sync_stack:
        cycle = sync_stack[sync_stack.index(external_name):] + [external_name]
        frappe.throw(
            f"Circular External Cost Center hierarchy: {' -> '.join(cycle)}"
        )
    sync_stack.append(external_name)

    external = frappe.get_doc("External Cost Center", external_name)
    sync_started_at = external.modified

    company = external.company
    company_abbr = frappe.get_cached_value("Company", company, "abbr") or ""

    # The ID we want the local Cost Center to have — must equal the
    # External Cost Center's name so references across doctypes stay consistent.
    target_name = external.name

    # Derive the display name (cost_center_name) by stripping the company
    # abbreviation suffix that ERPNext appends.  ERPNext's own naming rule
    # re-appends it, so we store only the bare label in cost_center_name.
    if company_abbr and target_name.endswith(f" - {company_abbr}"):
        cc_name = target_name[: -(len(company_abbr) + 3)]  # strip " - <abbr>"
    else:
        cc_name = target_name

    # -----------------------
    # Check if already exists
    # -----------------------
    # Fast path: look up by exact name (matches External Cost Center ID)
    if frappe.db.exists("Cost Center", target_name):
        external.db_set(
            "last_synced_on",
            sync_started_at,
            update_modified=False
        )
        return target_name

    # Legacy fallback: document may have been created before this fix with a
    # different auto-generated name — look it up by display name + company.
    existing = frappe.db.get_value(
        "Cost Center",
        {"cost_center_name": cc_name, "company": company},
        "name",
    )
    if existing:
        external.db_set(
            "last_synced_on",
            sync_started_at,
            update_modified=False
        )
        return existing

    # -----------------------
    # Handle Parent
    # -----------------------
    parent_cc = None

    if external.parent_cost_center:
        parent_external_name = resolve_tree_source_name(
            "External Cost Center",
            external.parent_cost_center,
            "remote_id",
        )
        if not parent_external_name:
            frappe.throw(
                f"Parent External Cost Center {external.parent_cost_center} "
                f"was not found for {external.name}"
            )
        parent_external = frappe.get_doc(
            "External Cost Center",
            parent_external_name,
        )

        parent_target_name = parent_external.name

        # Ensure the parent is created first (recursive, handles arbitrary depth)
        if not frappe.db.exists("Cost Center", parent_target_name):
            parent_cc = create_cost_center_from_external(
                parent_external_name,
                sync_stack=sync_stack,
            )
        else:
            parent_cc = parent_target_name
    else:
        parent_cc = _get_company_root_cost_center(company)

    # -----------------------
    # Create Cost Center
    # -----------------------
    doc = frappe.get_doc(
        {
            "doctype": "Cost Center",
            "cost_center_name": cc_name,
            "company": company,
            "parent_cost_center": parent_cc,
            "is_group": external.is_group,
            "disabled": external.disabled,
        }
    )

    # Force the document's primary key (name) to match the External Cost Center
    # ID exactly.  Without this, ERPNext's autoname rule generates the name as
    # "{cost_center_name} - {company_abbr}", which differs from the remote ID
    # when the remote already carries the suffix or uses a different format.
    doc.name = target_name
    doc.flags.name_set = True      # tells Frappe not to auto-generate the name
    doc.insert(ignore_permissions=True)

    external.db_set(
        "last_synced_on",
        sync_started_at,
        update_modified=False
    )

    return doc.name


def _get_company_root_cost_center(company):
    root = frappe.db.get_value(
        "Cost Center",
        {
            "company": company,
            "parent_cost_center": ["is", "not set"],
            "is_group": 1,
        },
        "name",
    )
    if root:
        return root

    frappe.throw(
        f"No root Cost Center found for company {company}. "
        "Please create the company Cost Center tree before syncing."
    )
