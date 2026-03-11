import frappe
import json
from frappe import _


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
    cc_name = external.cost_center_name

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

        parent_external_name = external.parent_cost_center

        if frappe.db.exists("External Cost Center", parent_external_name):

            parent_external = frappe.get_doc(
                "External Cost Center",
                parent_external_name
            )

            parent_cc = frappe.db.get_value(
                "Cost Center",
                {
                    "cost_center_name": parent_external.cost_center_name,
                    "company": company
                },
                "name"
            )

            if not parent_cc:
                parent_cc = create_cost_center_from_external(parent_external_name)

    # -----------------------
    # Create Cost Center
    # -----------------------
    doc = frappe.get_doc({
        "doctype": "Cost Center",
        "cost_center_name": external.cost_center_name,
        "company": external.company,
        "parent_cost_center": parent_cc,
        "is_group": external.is_group,
        "disabled": external.disabled
    })
    doc.flags.ignore_validate = True
    doc.flags.ignore_mandatory = True
    doc.insert(ignore_permissions=True)

    return doc.name