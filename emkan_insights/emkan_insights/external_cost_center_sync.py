import frappe
import json


@frappe.whitelist()
def sync_cost_center_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    created = []

    for name in names:
        cc = create_cost_center_from_external(name)
        created.append(cc)

    return created


def create_cost_center_from_external(external_name):

    external = frappe.get_doc("External Cost Center", external_name)

    company = external.company

    # Use external document name as the Cost Center name
    cc_name = external.name.replace(f" - {frappe.get_cached_value('Company', company, 'abbr')}", "")

    # -----------------------
    # Check if already exists
    # -----------------------
    existing = frappe.db.get_value(
        "Cost Center",
        {
            "cost_center_name": cc_name,
            "company": company
        },
        "name"
    )

    if existing:
        return existing

    parent_cc = None

    # -----------------------
    # Handle Parent
    # -----------------------
    if external.parent_cost_center:

        parent_external = frappe.get_doc(
            "External Cost Center",
            external.parent_cost_center
        )

        parent_cc_name = parent_external.name.replace(
            f" - {frappe.get_cached_value('Company', company, 'abbr')}",
            ""
        )

        parent_cc = frappe.db.get_value(
            "Cost Center",
            {
                "cost_center_name": parent_cc_name,
                "company": company
            },
            "name"
        )

        if not parent_cc:
            parent_cc = create_cost_center_from_external(
                external.parent_cost_center
            )

    # -----------------------
    # Create Cost Center
    # -----------------------
    doc = frappe.get_doc({
        "doctype": "Cost Center",
        "cost_center_name": cc_name,
        "company": company,
        "parent_cost_center": parent_cc,
        "is_group": external.is_group,
        "disabled": external.disabled
    })

    doc.flags.ignore_validate = True
    doc.flags.ignore_mandatory = True
    doc.insert(ignore_permissions=True)

    return doc.name