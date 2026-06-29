# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalBOM(Document):
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


@frappe.whitelist()
def sync_external_bom_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_bom = frappe.get_doc(source_doctype, name)

            # CHECK EXISTING
            existing_bom = frappe.db.get_value(
                "BOM", {"remote_id": ext_bom.remote_id}, "name"
            )

            if existing_bom:
                results.append({"name": existing_bom, "status": "exists"})
                continue

            bom = frappe.new_doc("BOM")
            bom.remote_id = ext_bom.remote_id

            if hasattr(bom, "source_site"):
                bom.source_site = ext_bom.source_site

            # COPY MAIN FIELDS
            for field, value in ext_bom.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "scrap_items", "operations", "exploded_items", "remote_id"]
                    and hasattr(bom, field)
                ):
                    bom.set(field, value)

            # ITEMS
            if hasattr(ext_bom, "items"):
                bom.set("items", [])
                for row in ext_bom.items:
                    item_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                            item_row[field] = value
                    if item_row.get("bom_no") and not frappe.db.exists("BOM", item_row["bom_no"]):
                        item_row["bom_no"] = None
                    bom.append("items", item_row)

            # SCRAP ITEMS
            if hasattr(ext_bom, "scrap_items"):
                bom.set("scrap_items", [])
                for row in ext_bom.scrap_items:
                    scrap_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS:
                            scrap_row[field] = value
                    bom.append("scrap_items", scrap_row)

            # OPERATIONS
            if hasattr(ext_bom, "operations"):
                bom.set("operations", [])
                for row in ext_bom.operations:
                    op_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS:
                            op_row[field] = value
                    if op_row.get("workstation") and not frappe.db.exists("Workstation", op_row["workstation"]):
                        op_row["workstation"] = None
                    if op_row.get("workstation_type") and not frappe.db.exists("Workstation Type", op_row["workstation_type"]):
                        op_row["workstation_type"] = None
                    bom.append("operations", op_row)

            # FLAGS
            bom.flags.ignore_permissions = True
            bom.flags.ignore_validate = True
            bom.flags.ignore_mandatory = True
            bom.flags.ignore_links = True

            # INSERT
            bom.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)

            # FORCE SAME NAME AS REMOTE ID
            target_name = ext_bom.remote_id

            if bom.name != target_name:
                frappe.db.sql("""
                    UPDATE `tabBOM` SET name = %s WHERE name = %s
                """, (target_name, bom.name))

                for child_table in [
                    "BOM Item", "BOM Scrap Item", "BOM Operation", "BOM Explosion Item"
                ]:
                    frappe.db.sql("""
                        UPDATE `tab{0}` SET parent = %s WHERE parent = %s
                    """.format(child_table), (target_name, bom.name))

                frappe.db.commit()
                bom.name = target_name

            # DOCSTATUS SYNC — RELOAD FIRST
            if bom.name != target_name:
                bom = frappe.get_doc("BOM", target_name)
            else:
                bom.reload()

            bom.flags.ignore_permissions = True
            bom.flags.ignore_validate = True
            bom.flags.ignore_mandatory = True
            bom.flags.ignore_links = True

            if ext_bom.docstatus == 1:
                bom.submit()

            elif ext_bom.docstatus == 2:
                bom.submit()
                bom = frappe.get_doc("BOM", target_name)
                bom.flags.ignore_permissions = True
                bom.cancel()

            results.append({"name": target_name, "status": "synced"})

        except Exception:
            frappe.log_error(f"BOM Sync Error: {name}", frappe.get_traceback())
            results.append({"name": name, "status": "failed"})

    return results