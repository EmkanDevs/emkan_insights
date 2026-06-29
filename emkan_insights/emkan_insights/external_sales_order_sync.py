# import frappe
# import json
# from frappe.utils import cint

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }

# IGNORE_ITEM_FIELDS = {
#     "quotation",
#     "quotation_item",
#     "prevdoc_doctype",
#     "prevdoc_docname",
#     "so_detail",
#     "against_sales_order"
# }

# DEFAULT_WAREHOUSE = "Stores - IMC"

# @frappe.whitelist()
# def sync_external_sales_order_docs(source_doctype, names):
#     if isinstance(names, str):
#         names = json.loads(names)

#     results = []

#     for name in names:
#         try:
#             ext_so = frappe.get_doc(source_doctype, name)
#             target_name = ext_so.name 

#             if frappe.db.exists("Sales Order", target_name):
#                 results.append({"name": target_name, "status": "exists"})
#                 continue

#             so = frappe.new_doc("Sales Order")
            
#             so.name = target_name
#             so.remote_id = target_name
            
#             # --- BYPASS FLAGS START ---
#             so.flags.ignore_permissions = True
#             so.flags.ignore_mandatory = True
#             so.flags.ignore_links = True
#             so.flags.ignore_validate = True  # This stops the "Could not find Cost Center/Tax" error
#             # --- BYPASS FLAGS END ---

#             # Copy Main Fields
#             for field, value in ext_so.as_dict().items():
#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "taxes", "remote_id"]
#                     and hasattr(so, field)
#                 ):
#                     so.set(field, value)

#             # Items & Taxes
#             so.set("items", [])
#             for row in ext_so.items:
#                 item_row = {f: v for f, v in row.as_dict().items() if f not in SYSTEM_FIELDS and f not in IGNORE_ITEM_FIELDS}
#                 if not item_row.get("warehouse"):
#                     item_row["warehouse"] = DEFAULT_WAREHOUSE
#                 so.append("items", item_row)

#             if hasattr(ext_so, "taxes"):
#                 so.set("taxes", [])
#                 for row in ext_so.taxes:
#                     tax_row = {f: v for f, v in row.as_dict().items() if f not in SYSTEM_FIELDS}
#                     so.append("taxes", tax_row)

#             # Manual DB Insert to preserve custom name
#             so.db_insert()            
#             for table_field in ["items", "taxes"]:
#                 for d in so.get(table_field):
#                     d.parent = so.name
#                     d.parenttype = "Sales Order"
#                     d.parentfield = table_field
#                     d.db_insert()

#             # Handling Docstatus (Submission)
#             target_status = cint(ext_so.docstatus)
            
#             if target_status > 0:
#                 # Reload the document we just inserted
#                 updated_so = frappe.get_doc("Sales Order", target_name)
                
#                 # RE-APPLY BYPASS FLAGS for the submission phase
#                 updated_so.flags.ignore_permissions = True
#                 updated_so.flags.ignore_links = True
#                 updated_so.flags.ignore_validate = True
                
#                 if target_status == 1:
#                     updated_so.submit()
#                 elif target_status == 2:
#                     updated_so.submit()
#                     updated_so.cancel()
                
#                 final_name = updated_so.name
#                 final_status = updated_so.docstatus
#             else:
#                 final_name = so.name
#                 final_status = 0

#             results.append({"name": final_name, "status": "synced", "docstatus": final_status})
#             frappe.db.commit()

#         except Exception as e:
#             frappe.db.rollback()
#             frappe.log_error(title=f"SO Sync Fail: {name}", message=frappe.get_traceback())
#             results.append({"name": name, "status": "failed", "error": str(e)})

#     return results

#########################################################################################

#######                                 NEW                         ############

import frappe
import json
from frappe.utils import cint

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


@frappe.whitelist()
def sync_external_sales_order_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_so = frappe.get_doc(source_doctype, name)
            target_name = ext_so.name

            # -----------------------------
            # SKIP IF EXISTS
            # -----------------------------
            if frappe.db.exists("Sales Order", target_name):
                results.append({"name": target_name, "status": "exists"})
                continue

            so = frappe.new_doc("Sales Order")

            # -----------------------------
            # SET REMOTE ID AS NAME
            # -----------------------------
            so.name = target_name
            so.set("__newname", target_name)
            so.flags.name_set = True
            so.remote_id = target_name

            # -----------------------------
            # FLAGS
            # -----------------------------
            so.flags.ignore_permissions = True
            so.flags.ignore_mandatory = True
            so.flags.ignore_links = True
            so.flags.ignore_validate = True

            # -----------------------------
            # COPY MAIN FIELDS
            # -----------------------------
            for field, value in ext_so.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "remote_id"]
                    and hasattr(so, field)
                ):
                    so.set(field, value)

            # -----------------------------
            # ITEMS
            # -----------------------------
            so.set("items", [])

            for row in ext_so.items:
                item_code = row.get("item_code")

                # Ensure item exists
                if not frappe.db.exists("Item", item_code):
                    frappe.get_doc({
                        "doctype": "Item",
                        "item_code": item_code,
                        "item_name": row.get("item_name") or item_code,
                        "item_group": "All Item Groups",
                        "stock_uom": row.get("uom") or "Nos"
                    }).insert(ignore_permissions=True)

                # SAFE item_name
                item_name = (
                    frappe.db.get_value("Item", item_code, "item_name")
                    or row.get("item_name")
                    or item_code
                )

                so.append("items", {
                    "item_code": item_code,
                    "item_name": item_name,
                    "qty": row.get("qty") or 1,
                    "rate": row.get("rate") or 0,
                    "uom": row.get("uom") or "Nos",
                    "stock_uom": row.get("stock_uom") or row.get("uom") or "Nos",
                    "conversion_factor": row.get("conversion_factor") or 1,
                    "warehouse": row.get("warehouse") or DEFAULT_WAREHOUSE,
                    "delivery_date": row.get("delivery_date"),
                    "description": item_name
                })

            # -----------------------------
            # TAXES
            # -----------------------------
            if hasattr(ext_so, "taxes"):
                so.set("taxes", [])
                for row in ext_so.taxes:
                    so.append("taxes", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })

            # -----------------------------
            # INSERT (NO DUPLICATES)
            # -----------------------------
            so.db_insert()
            
            for df in so.meta.get_table_fields():
                for child_row in so.get(df.fieldname) or []:
                    child_row.parent = so.name
                    child_row.parenttype = so.doctype
                    child_row.parentfield = df.fieldname
                    child_row.name = frappe.generate_hash(length=10)
                    child_row.db_insert()

            # ⭐ CRITICAL FIX (item_name issue)
            frappe.db.sql("""
                UPDATE `tabSales Order Item` soi
                LEFT JOIN `tabItem` i ON soi.item_code = i.name
                SET soi.item_name = COALESCE(i.item_name, soi.item_code)
                WHERE soi.parent = %s
            """, (target_name,))

            # -----------------------------
            # DOCSTATUS SYNC
            # -----------------------------
            target_status = cint(ext_so.docstatus)

            if target_status > 0:
                updated_so = frappe.get_doc("Sales Order", target_name)

                updated_so.flags.ignore_permissions = True
                updated_so.flags.ignore_links = True
                updated_so.flags.ignore_validate = True

                if target_status == 1:
                    updated_so.submit()

                elif target_status == 2:
                    updated_so.submit()
                    updated_so.cancel()

                final_name = updated_so.name
                final_status = updated_so.docstatus
            else:
                final_name = so.name
                final_status = 0

            results.append({
                "name": final_name,
                "status": "synced",
                "docstatus": final_status
            })

            frappe.db.commit()

        except Exception as e:
            frappe.db.rollback()

            frappe.log_error(
                title=f"SO Sync Fail: {name}",
                message=frappe.get_traceback()
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results