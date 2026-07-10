# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalProductionPlan(Document):
	pass

# import frappe
from frappe.model.document import Document


class ExternalProductionPlan(Document):
	pass

import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation", "quotation_item", "prevdoc_doctype",
    "prevdoc_docname", "so_detail", "against_sales_order"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


def _get_company_abbr(company):
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(f"Company '{company}' has no abbreviation set")
    return abbr


def _build_target_name(company_abbr, base_id):
    base_id = (base_id or "").strip()
    if not base_id:
        frappe.throw("Missing base id for Production Plan naming")

    if base_id.startswith(f"{company_abbr}-"):
        return base_id

    return f"{company_abbr}-{base_id}"


def _prefix_reference(company_abbr, reference_name):
    reference_name = (reference_name or "").strip()
    if not reference_name:
        return None

    if reference_name.startswith(f"{company_abbr}-"):
        return reference_name

    return f"{company_abbr}-{reference_name}"


# def _normalize_fingerprint_value(value):
#     if value is None:
#         return ""

#     if hasattr(value, "isoformat"):
#         return value.isoformat()

#     if isinstance(value, str):
#         return value.strip()

#     return value


# def _get_po_item_fingerprint(row, item_row):
#     remote_row_id = item_row.get("custom_remote_id") or row.get("custom_remote_id")
#     if remote_row_id:
#         return ("remote_id", str(remote_row_id).strip())

#     return (
#         "fields",
#         json.dumps(
#             {
#                 field: _normalize_fingerprint_value(value)
#                 for field, value in sorted(item_row.items())
#                 if field not in {"idx"}
#             },
#             sort_keys=True,
#             default=str,
#         ),
#     )


@frappe.whitelist()
def sync_external_production_plan_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_pp = frappe.get_doc(source_doctype, name)
            company_abbr = _get_company_abbr(ext_pp.company)
            target_name = _build_target_name(company_abbr, ext_pp.remote_id)

            # CHECK EXISTING
            existing_pp = frappe.db.get_value(
                "Production Plan", {"remote_id": ext_pp.remote_id}, "name"
            ) or (target_name if frappe.db.exists("Production Plan", target_name) else None)

            if existing_pp:
                results.append({"name": existing_pp, "status": "exists"})
                continue

            pp = frappe.new_doc("Production Plan")
            pp.remote_id = ext_pp.remote_id

            if hasattr(pp, "source_site"):
                pp.source_site = ext_pp.source_site

            # COPY MAIN FIELDS
            for field, value in ext_pp.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in [
                        "po_items",           # Assembly Items
                        "sales_orders",        # Sales Orders
                        "material_requests",   # Material Requests
                        "mr_items",            # Raw Materials
                        "sub_assembly_items",  # Sub Assembly Items
                        "skip_available_sub_assembly_item",
                        "remote_id"
                    ]
                    and hasattr(pp, field)
                ):
                    pp.set(field, value)

            # SALES ORDERS (sales_orders)
            if hasattr(ext_pp, "sales_orders"):
                pp.set("sales_orders", [])
                for row in ext_pp.sales_orders:
                    so_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            so_row[field] = value
                    pp.append("sales_orders", so_row)

            # MATERIAL REQUESTS (material_requests)
            if hasattr(ext_pp, "material_requests"):
                pp.set("material_requests", [])
                for row in ext_pp.material_requests:
                    mr_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            mr_row[field] = value
                    pp.append("material_requests", mr_row)

            # ASSEMBLY ITEMS (po_items in Production Plan)
            # if hasattr(ext_pp, "po_items"):
            #     pp.set("po_items", [])
            #     seen_po_items = set()
            #     for row in ext_pp.po_items:
            #         item_row = {}
            #         for field, value in row.as_dict().items():
            #             if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
            #                 item_row[field] = value
            #         # Validate BOM reference
            #         if item_row.get("bom_no"):
            #             item_row["bom_no"] = _prefix_reference(company_abbr, item_row["bom_no"])

            #         if item_row.get("bom_no") and not frappe.db.exists("BOM", item_row["bom_no"]):
            #             item_row["bom_no"] = None

            #         fingerprint = _get_po_item_fingerprint(row, item_row)
            #         if fingerprint in seen_po_items:
            #             continue
            #         seen_po_items.add(fingerprint)

            #         pp.append("po_items", item_row)
            
            # ASSEMBLY ITEMS (po_items in Production Plan)
            if hasattr(ext_pp, "po_items"):
                pp.set("po_items", [])
                for row in ext_pp.po_items:
                    item_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            item_row[field] = value

                    # Validate BOM reference
                    if item_row.get("bom_no"):
                        item_row["bom_no"] = _prefix_reference(company_abbr, item_row["bom_no"])

                    if (
                        item_row.get("bom_no")
                        and not frappe.db.exists("BOM", item_row["bom_no"])
                    ):
                        item_row["bom_no"] = None

                    pp.append("po_items", item_row)

            # SUB ASSEMBLY ITEMS (sub_assembly_items)
            if hasattr(ext_pp, "sub_assembly_items"):
                pp.set("sub_assembly_items", [])
                for row in ext_pp.sub_assembly_items:
                    sub_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            sub_row[field] = value
                    # Validate BOM reference
                    if sub_row.get("bom_no") and not frappe.db.exists("BOM", sub_row["bom_no"]):
                        sub_row["bom_no"] = None
                    # Validate item reference
                    if sub_row.get("production_plan_item") and not frappe.db.exists("Production Plan Item", sub_row["production_plan_item"]):
                        sub_row["production_plan_item"] = None
                    pp.append("sub_assembly_items", sub_row)

            # RAW MATERIALS (mr_items in Production Plan)
            if hasattr(ext_pp, "mr_items"):
                pp.set("mr_items", [])
                for row in ext_pp.mr_items:
                    raw_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            raw_row[field] = value
                    pp.append("mr_items", raw_row)

            # FLAGS
            pp.flags.ignore_permissions = True
            pp.flags.ignore_validate = True
            pp.flags.ignore_mandatory = True
            pp.flags.ignore_links = True

            # INSERT
            pp.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)

            # FORCE COMPANY-PREFIXED REMOTE ID

            if pp.name != target_name:
                frappe.db.sql("""
                    UPDATE `tabProduction Plan` SET name = %s WHERE name = %s
                """, (target_name, pp.name))

                # Update child table parent references
                for child_table in [
                    "Production Plan Item",           # po_items
                    "Production Plan Sales Order",     # sales_orders
                    "Production Plan Material Request", # material_requests
                    "Production Plan Item Reference",   # item references
                    "Production Plan Sub Assembly Item", # sub_assembly_items
                    "Material Request Plan Item"        # mr_items
                ]:
                    frappe.db.sql("""
                        UPDATE `tab{0}` SET parent = %s WHERE parent = %s
                    """.format(child_table), (target_name, pp.name))

                frappe.db.commit()
                pp.name = target_name

            # DOCSTATUS SYNC — RELOAD FIRST
            if pp.name != target_name:
                pp = frappe.get_doc("Production Plan", target_name)
            else:
                pp.reload()

            pp.flags.ignore_permissions = True
            pp.flags.ignore_validate = True
            pp.flags.ignore_mandatory = True
            pp.flags.ignore_links = True

            if ext_pp.docstatus == 1:
                pp.submit()

            elif ext_pp.docstatus == 2:
                pp.submit()
                pp = frappe.get_doc("Production Plan", target_name)
                pp.flags.ignore_permissions = True
                pp.cancel()

            results.append({"name": target_name, "status": "synced"})

        except Exception:
            frappe.log_error(f"Production Plan Sync Error: {name}", frappe.get_traceback())
            results.append({"name": name, "status": "failed"})

    return results
