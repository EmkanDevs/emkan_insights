# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document


class ExternalProductionPlan(Document):
	pass



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


def _deduplicate_child_table(parent_name, child_table, key_field="custom_remote_id"):
    """Remove duplicate child rows based on key_field."""
    rows = frappe.get_all(child_table, 
        filters={"parent": parent_name}, 
        fields=["name", key_field],
        order_by="creation asc"
    )
    seen = set()
    to_delete = []
    for row in rows:
        key = row.get(key_field) or row.name
        if key in seen:
            to_delete.append(row.name)
        else:
            seen.add(key)
    
    for name in to_delete:
        frappe.db.sql(f"DELETE FROM `tab{child_table}` WHERE name = %s", (name,))
    
    if to_delete:
        frappe.db.commit()


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
                        "po_items",
                        "sales_orders",
                        "material_requests",
                        "mr_items",
                        "sub_assembly_items",
                        "skip_available_sub_assembly_item",
                        "remote_id"
                    ]
                    and hasattr(pp, field)
                ):
                    pp.set(field, value)

            # SALES ORDERS
            if hasattr(ext_pp, "sales_orders"):
                pp.set("sales_orders", [])
                for row in ext_pp.sales_orders:
                    so_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            so_row[field] = value
                    pp.append("sales_orders", so_row)

            # MATERIAL REQUESTS
            if hasattr(ext_pp, "material_requests"):
                pp.set("material_requests", [])
                for row in ext_pp.material_requests:
                    mr_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            mr_row[field] = value
                    pp.append("material_requests", mr_row)

            # PO ITEMS
            if hasattr(ext_pp, "po_items"):
                pp.set("po_items", [])
                for row in ext_pp.po_items:
                    item_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            item_row[field] = value

                    if item_row.get("bom_no"):
                        item_row["bom_no"] = _prefix_reference(company_abbr, item_row["bom_no"])

                    if (
                        item_row.get("bom_no")
                        and not frappe.db.exists("BOM", item_row["bom_no"])
                    ):
                        item_row["bom_no"] = None

                    pp.append("po_items", item_row)

            # SUB ASSEMBLY ITEMS
            if hasattr(ext_pp, "sub_assembly_items"):
                pp.set("sub_assembly_items", [])
                for row in ext_pp.sub_assembly_items:
                    sub_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            sub_row[field] = value
                    if sub_row.get("bom_no") and not frappe.db.exists("BOM", sub_row.get("bom_no")):
                        sub_row["bom_no"] = None
                    if sub_row.get("production_plan_item") and not frappe.db.exists("Production Plan Item", sub_row.get("production_plan_item")):
                        sub_row["production_plan_item"] = None
                    pp.append("sub_assembly_items", sub_row)

            # RAW MATERIALS
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
                    "Production Plan Item",
                    "Production Plan Sales Order",
                    "Production Plan Material Request",
                    "Production Plan Item Reference",
                    "Production Plan Sub Assembly Item",
                    "Material Request Plan Item"
                ]:
                    frappe.db.sql("""
                        UPDATE `tab{0}` SET parent = %s WHERE parent = %s
                    """.format(child_table), (target_name, pp.name))

                frappe.db.commit()

            # SAFETY NET: Deduplicate child tables
            _deduplicate_child_table(target_name, "Production Plan Item", "custom_remote_id")
            _deduplicate_child_table(target_name, "Production Plan Sales Order", "sales_order")
            _deduplicate_child_table(target_name, "Production Plan Material Request", "material_request")
            _deduplicate_child_table(target_name, "Production Plan Sub Assembly Item", "production_plan_item")
            _deduplicate_child_table(target_name, "Material Request Plan Item", "item_code")

            # DOCSTATUS SYNC
            pp = frappe.get_doc("Production Plan", target_name)
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
                pp.flags.ignore_validate = True
                pp.flags.ignore_mandatory = True
                pp.flags.ignore_links = True
                pp.cancel()

            results.append({"name": target_name, "status": "synced"})

        except Exception:
            frappe.log_error(f"Production Plan Sync Error: {name}", frappe.get_traceback())
            results.append({"name": name, "status": "failed"})

    return results