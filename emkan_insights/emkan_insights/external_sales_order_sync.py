# import frappe
# import json
# from frappe.utils import cint

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
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

#             # -----------------------------
#             # SKIP IF EXISTS
#             # -----------------------------
#             if frappe.db.exists("Sales Order", target_name):
#                 results.append({"name": target_name, "status": "exists"})
#                 continue

#             so = frappe.new_doc("Sales Order")

#             # -----------------------------
#             # SET REMOTE ID AS NAME
#             # -----------------------------
#             so.name = target_name
#             so.set("__newname", target_name)
#             so.flags.name_set = True
#             so.remote_id = target_name

#             # -----------------------------
#             # FLAGS
#             # -----------------------------
#             so.flags.ignore_permissions = True
#             so.flags.ignore_mandatory = True
#             so.flags.ignore_links = True
#             so.flags.ignore_validate = True

#             # -----------------------------
#             # COPY MAIN FIELDS
#             # -----------------------------
#             for field, value in ext_so.as_dict().items():
#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "taxes", "remote_id"]
#                     and hasattr(so, field)
#                 ):
#                     so.set(field, value)

#             # -----------------------------
#             # ITEMS
#             # -----------------------------
#             so.set("items", [])

#             for row in ext_so.items:
#                 item_code = row.get("item_code")

#                 # Ensure item exists
#                 if not frappe.db.exists("Item", item_code):
#                     frappe.get_doc({
#                         "doctype": "Item",
#                         "item_code": item_code,
#                         "item_name": row.get("item_name") or item_code,
#                         "item_group": "All Item Groups",
#                         "stock_uom": row.get("uom") or "Nos"
#                     }).insert(ignore_permissions=True)

#                 # SAFE item_name
#                 item_name = (
#                     frappe.db.get_value("Item", item_code, "item_name")
#                     or row.get("item_name")
#                     or item_code
#                 )

#                 so.append("items", {
#                     "item_code": item_code,
#                     "item_name": item_name,
#                     "qty": row.get("qty") or 1,
#                     "rate": row.get("rate") or 0,
#                     "uom": row.get("uom") or "Nos",
#                     "stock_uom": row.get("stock_uom") or row.get("uom") or "Nos",
#                     "conversion_factor": row.get("conversion_factor") or 1,
#                     "warehouse": row.get("warehouse") or DEFAULT_WAREHOUSE,
#                     "delivery_date": row.get("delivery_date"),
#                     "description": item_name
#                 })

#             # -----------------------------
#             # TAXES
#             # -----------------------------
#             if hasattr(ext_so, "taxes"):
#                 so.set("taxes", [])
#                 for row in ext_so.taxes:
#                     so.append("taxes", {
#                         f: v for f, v in row.as_dict().items()
#                         if f not in SYSTEM_FIELDS
#                     })

#             # -----------------------------
#             # INSERT (NO DUPLICATES)
#             # -----------------------------
#             so.db_insert()
            
#             for df in so.meta.get_table_fields():
#                 for child_row in so.get(df.fieldname) or []:
#                     child_row.parent = so.name
#                     child_row.parenttype = so.doctype
#                     child_row.parentfield = df.fieldname
#                     child_row.name = frappe.generate_hash(length=10)
#                     child_row.db_insert()

#             # ⭐ CRITICAL FIX (item_name issue)
#             frappe.db.sql("""
#                 UPDATE `tabSales Order Item` soi
#                 LEFT JOIN `tabItem` i ON soi.item_code = i.name
#                 SET soi.item_name = COALESCE(i.item_name, soi.item_code)
#                 WHERE soi.parent = %s
#             """, (target_name,))

#             # -----------------------------
#             # DOCSTATUS SYNC
#             # -----------------------------
#             target_status = cint(ext_so.docstatus)

#             if target_status > 0:
#                 updated_so = frappe.get_doc("Sales Order", target_name)

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

#             results.append({
#                 "name": final_name,
#                 "status": "synced",
#                 "docstatus": final_status
#             })

#             frappe.db.commit()

#         except Exception as e:
#             frappe.db.rollback()

#             frappe.log_error(
#                 title=f"SO Sync Fail: {name}",
#                 message=frappe.get_traceback()
#             )

#             results.append({
#                 "name": name,
#                 "status": "failed",
#                 "error": str(e)
#             })

#     return results


import frappe
import json
from frappe.utils import cint, flt

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
            remote_id = ext_so.name

            # ------------------------------------------------
            # BUILD TARGET NAME: {company_abbr}-{remote_id}
            # Same pattern as Work Order sync
            # ------------------------------------------------
            company_abbr = frappe.db.get_value('Company', ext_so.company, 'abbr') or ''
            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # CHECK EXISTING (by remote_id, same as Work Order)
            # ------------------------------------------------
            existing_so = frappe.db.get_value(
                "Sales Order",
                {"remote_id": remote_id},
                "name"
            )

            if existing_so:
                results.append({
                    "name": existing_so,
                    "status": "exists"
                })
                continue

            so = frappe.new_doc("Sales Order")

            # ------------------------------------------------
            # SET NAME WITH COMPANY ABBR PREFIX
            # Same pattern as Work Order
            # ------------------------------------------------
            so.remote_id = remote_id
            so.name = target_name
            so.flags.name_set = True

            src_site_value = getattr(ext_so, "site_url", None) or getattr(ext_so, "source_site", None)
            if src_site_value:
                mapped = False
                for site_field in ("source_site", "site_url"):
                    if hasattr(so, site_field):
                        so.set(site_field, src_site_value)
                        mapped = True
                        break
                if not mapped:
                    frappe.log_error(
                        f"SO Sync: source_site value '{src_site_value}' not mapped — "
                        f"Sales Order missing 'source_site'/'site_url' Custom Field.",
                        "SO Sync: missing source_site field"
                    )

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            so.flags.ignore_permissions = True
            so.flags.ignore_mandatory = True
            so.flags.ignore_links = True
            so.flags.ignore_validate = True
            so.flags.ignore_naming_series = True

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # NOTE: All child-table fields (items, taxes,
            # payment_schedule, etc.) must be excluded here and
            # rebuilt explicitly below. Otherwise Frappe tries to
            # reuse the source doc's child row names, which
            # already exist in the DB -> DuplicateEntryError.
            # ------------------------------------------------
            for field, value in ext_so.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "payment_schedule", "remote_id"]
                    and hasattr(so, field)
                ):
                    so.set(field, value)

            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
            so.set("items", [])
            conversion_rate = flt(getattr(ext_so, "conversion_rate", 0)) or 1.0

            for row in ext_so.items:
                item_code = row.get("item_code")

                if not frappe.db.exists("Item", item_code):
                    frappe.get_doc({
                        "doctype": "Item",
                        "item_code": item_code,
                        "item_name": row.get("item_name") or item_code,
                        "item_group": "All Item Groups",
                        "stock_uom": row.get("uom") or "Nos"
                    }).insert(ignore_permissions=True)

                item_name = (
                    frappe.db.get_value("Item", item_code, "item_name")
                    or row.get("item_name")
                    or item_code
                )

                qty    = flt(row.get("qty")) or 1
                rate   = flt(row.get("rate")) or 0
                amount = flt(row.get("amount")) if flt(row.get("amount")) else qty * rate

                so.append("items", {
                    "item_code":            item_code,
                    "item_name":            item_name,
                    "description":          row.get("description") or item_name,
                    "custom_remote_id":     row.get("custom_remote_id"),  
                    "qty":                  qty,
                    "stock_qty":            qty,
                    "rate":                 rate,
                    "amount":               amount,
                    "net_rate":             flt(row.get("net_rate")) or rate,
                    "net_amount":           flt(row.get("net_amount")) or amount,
                    "base_rate":            rate * conversion_rate,
                    "base_amount":          amount * conversion_rate,
                    "base_net_rate":        (flt(row.get("net_rate")) or rate) * conversion_rate,
                    "base_net_amount":      (flt(row.get("net_amount")) or amount) * conversion_rate,
                    "price_list_rate":      flt(row.get("price_list_rate")),
                    "base_price_list_rate": flt(row.get("base_price_list_rate")),
                    "discount_percentage":  flt(row.get("discount_percentage")),
                    "discount_amount":      flt(row.get("discount_amount")),
                    "item_tax_rate":        row.get("item_tax_rate") or "{}",
                    "uom":                  row.get("uom") or "Nos",
                    "stock_uom":            row.get("stock_uom") or row.get("uom") or "Nos",
                    "conversion_factor":    row.get("conversion_factor") or 1,
                    "warehouse":            row.get("warehouse") or DEFAULT_WAREHOUSE,
                    "delivery_date":        row.get("delivery_date"),
                    "cost_center":          row.get("cost_center"),
                    "project":              row.get("project") or getattr(ext_so, "project", None),
                })

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_so, "taxes"):
                so.set("taxes", [])
                for row in ext_so.taxes:
                    so.append("taxes", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })

            # ------------------------------------------------
            # PAYMENT SCHEDULE
            # Rebuilt explicitly (like taxes) so that stale child
            # row names from the source doc are stripped out and
            # Frappe generates fresh names for the new doc.
            # ------------------------------------------------
            if hasattr(ext_so, "payment_schedule"):
                so.set("payment_schedule", [])
                for row in ext_so.payment_schedule:
                    so.append("payment_schedule", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })
            # ------------------------------------------------
            so.total                = flt(getattr(ext_so, "total", 0))
            so.net_total             = flt(getattr(ext_so, "net_total", 0)) or so.total
            so.base_total            = flt(getattr(ext_so, "base_total", 0))
            so.base_net_total        = flt(getattr(ext_so, "base_net_total", 0)) or so.base_total
            so.total_qty             = flt(getattr(ext_so, "total_qty", 0))
            so.grand_total           = flt(getattr(ext_so, "grand_total", 0))
            so.base_grand_total      = flt(getattr(ext_so, "base_grand_total", 0))
            so.total_taxes_and_charges      = flt(getattr(ext_so, "total_taxes_and_charges", 0))
            so.base_total_taxes_and_charges = flt(getattr(ext_so, "base_total_taxes_and_charges", 0))
            so.rounding_adjustment          = flt(getattr(ext_so, "rounding_adjustment", 0))
            so.base_rounding_adjustment      = flt(getattr(ext_so, "base_rounding_adjustment", 0))
            so.rounded_total                 = flt(getattr(ext_so, "rounded_total", 0))
            so.base_rounded_total            = flt(getattr(ext_so, "base_rounded_total", 0))
            so.in_words                      = getattr(ext_so, "in_words", "")
            so.base_in_words                 = getattr(ext_so, "base_in_words", "")
            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            so.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # ------------------------------------------------
            # NAMING SAFETY NET: If Frappe renamed it
            # Same pattern as Work Order
            # ------------------------------------------------
            if so.name != target_name:

                if not frappe.db.exists("Sales Order", target_name):
                    frappe.db.sql("""
                        UPDATE `tabSales Order`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, so.name))

                old_name = so.name

                child_tables = [
                    "Sales Order Item",
                    "Sales Order Tax",
                    "Payment Schedule",
                ]

                for child_table in child_tables:
                    if frappe.db.exists("DocType", child_table):
                        frappe.db.sql("""
                            UPDATE `tab{0}`
                            SET parent = %s
                            WHERE parent = %s
                        """.format(child_table), (target_name, old_name))

                so.name = target_name

            frappe.db.commit()

            # ------------------------------------------------
            # VERIFY: confirm items actually landed
            # ------------------------------------------------
            actual_item_count = frappe.db.count("Sales Order Item", {"parent": target_name})
            expected_item_count = len(ext_so.get("items") or [])

            if actual_item_count != expected_item_count:
                frappe.log_error(
                    f"Sales Order {target_name}: expected {expected_item_count} "
                    f"items, found {actual_item_count} after insert.",
                    "ExternalSalesOrder Sync - items mismatch"
                )

            # ------------------------------------------------
            # DOCSTATUS SYNC
            # ------------------------------------------------
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
                    updated_so = frappe.get_doc("Sales Order", target_name)
                    updated_so.flags.ignore_permissions = True
                    updated_so.flags.ignore_links = True
                    updated_so.flags.ignore_validate = True
                    updated_so.cancel()

                # Safety net after submit/cancel
                if updated_so.name != target_name:
                    old_name = updated_so.name

                    if not frappe.db.exists("Sales Order", target_name):
                        frappe.db.sql("""
                            UPDATE `tabSales Order`
                            SET name = %s
                            WHERE name = %s
                        """, (target_name, old_name))

                    for child_table in ["Sales Order Item", "Sales Order Tax", "Payment Schedule"]:
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""
                                UPDATE `tab{0}`
                                SET parent = %s
                                WHERE parent = %s
                            """.format(child_table), (target_name, old_name))

                    updated_so.name = target_name

                final_name = updated_so.name
                final_status = updated_so.docstatus
            else:
                final_name = so.name
                final_status = 0

            results.append({
                "name": final_name,
                "status": "synced",
                "docstatus": final_status,
                "items_count": actual_item_count
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