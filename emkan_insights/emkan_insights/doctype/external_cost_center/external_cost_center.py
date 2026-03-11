# Copyright (c) 2026, Mukesh Variyani and contributors

import frappe
from frappe.utils import cint
from frappe.utils.nestedset import NestedSet


class ExternalCostCenter(NestedSet):
    nsm_parent_field = "parent_cost_center"


@frappe.whitelist()
def sync_external_records(names):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(source_doctype="External Cost Center", names=names)


@frappe.whitelist()
def get_external_cost_center_children(doctype, parent=None, company=None, is_root=False):

    filters = [["docstatus", "<", 2]]

    # Root Nodes
    if is_root:
        filters.append(["ifnull(parent_cost_center,'')", "=", ""])

        if company:
            filters.append(["company", "=", company])

    # Child Nodes
    else:
        filters.append(["parent_cost_center", "=", parent])

    cost_centers = frappe.get_all(
        "External Cost Center",
        fields=[
            "name",
            "cost_center_name",
            "parent_cost_center",
            "is_group"
        ],
        filters=filters,
        order_by="name asc"
    )

    data = []

    for cc in cost_centers:
        data.append({
            "value": cc.name,
            "title": cc.cost_center_name,
            "expandable": cint(cc.is_group),
            "parent": cc.parent_cost_center
        })

    return data


@frappe.whitelist()
def add_external_cost_center(args=None):

    from frappe.desk.treeview import make_tree_args

    if not args:
        args = frappe.local.form_dict

    args.doctype = "External Cost Center"
    args = make_tree_args(**args)

    external_cost_center = frappe.new_doc("External Cost Center")

    if args.get("ignore_permissions"):
        external_cost_center.flags.ignore_permissions = True
        args.pop("ignore_permissions")

    external_cost_center.update(args)

    parent = args.get("parent")

    if parent:
        external_cost_center.parent_cost_center = parent
        external_cost_center.old_parent = parent
    else:
        external_cost_center.parent_cost_center = None
        external_cost_center.old_parent = None

    if cint(args.get("is_root")):
        external_cost_center.parent_cost_center = None
        external_cost_center.flags.ignore_mandatory = True

    external_cost_center.insert()

    return external_cost_center.name