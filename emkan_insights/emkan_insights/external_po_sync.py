# import frappe
# import json


# # ==========================================================
# # ENTRY POINT
# # ==========================================================

# @frappe.whitelist()
# def sync_purchase_order_docs(source_doctype: str, names):

#     if isinstance(names, str):
#         names = json.loads(names)

#     frappe.enqueue(
#         "emkan_insights.emkan_insights.external_po_sync.sync_bulk_purchase_orders",
#         queue="long",
#         names=names,
#         timeout=2000
#     )

#     return f"{len(names)} Purchase Orders queued for sync"


# # ==========================================================
# # BULK SYNC
# # ==========================================================

# def sync_bulk_purchase_orders(names):

#     sources = frappe.get_all(
#         "External Purchase Order",
#         filters={"name": ["in", names]},
#         fields=["name", "remote_id"],
#         limit_page_length=0
#     )

#     full_docs = {
#         d.name: frappe.get_doc("External Purchase Order", d.name)
#         for d in sources
#     }

#     # ------------------------------------------------------
#     # PRELOAD
#     # ------------------------------------------------------
#     suppliers = set(frappe.get_all("Supplier", pluck="name", limit_page_length=0))
#     projects = set(frappe.get_all("Project", pluck="name", limit_page_length=0))
#     cost_centers = set(frappe.get_all("Cost Center", pluck="name", limit_page_length=0))

#     remote_ids = [d.remote_id for d in sources if d.remote_id]

#     existing_pos = frappe.get_all(
#         "Purchase Order",
#         filters={"remote_id": ["in", remote_ids]},
#         fields=["name", "remote_id", "docstatus"],
#         limit_page_length=0
#     )

#     existing_map = {d.remote_id: d for d in existing_pos}

#     success = 0
#     failed = 0

#     # ------------------------------------------------------
#     # LOOP
#     # ------------------------------------------------------
#     for idx, name in enumerate(names, start=1):

#         try:
#             src = full_docs.get(name)

#             if not src:
#                 continue

#             if not src.company:
#                 raise Exception(f"Company missing in External PO: {name}")

#             existing = existing_map.get(src.remote_id)

#             # ------------------------------------------------------
#             # UPSERT
#             # ------------------------------------------------------
#             doc = upsert_purchase_order(
#                 src,
#                 existing_name=existing["name"] if existing else None,
#                 suppliers=suppliers,
#                 projects=projects,
#                 cost_centers=cost_centers
#             )

#             # ------------------------------------------------------
#             # DOCSTATUS
#             # ------------------------------------------------------
#             sync_docstatus(doc, src.docstatus)

#             success += 1

#         except Exception as e:
#             failed += 1

#             frappe.log_error(
#                 title="Purchase Order Sync Failed",
#                 message=f"""
# External PO: {name}
# Remote ID: {getattr(src, 'remote_id', '')}
# Error: {str(e)}
# """
#             )

#         if idx % 20 == 0:
#             frappe.db.commit()

#     frappe.db.commit()

#     return {"success": success, "failed": failed}


# # ==========================================================
# # UPSERT
# # ==========================================================

# def upsert_purchase_order(src, existing_name=None, suppliers=None, projects=None, cost_centers=None):

#     if existing_name:
#         doc = frappe.get_doc("Purchase Order", existing_name)
#     else:
#         doc = frappe.new_doc("Purchase Order")

#         # ✅ FORCE NAME = remote_id
#         if src.remote_id:
#             if frappe.db.exists("Purchase Order", src.remote_id):
#                 return frappe.get_doc("Purchase Order", src.remote_id)

#             doc.name = src.remote_id
#             doc.flags.name_set = True

#     # ------------------------------------------------------
#     # VALIDATIONS
#     # ------------------------------------------------------
#     if src.supplier and src.supplier not in suppliers:
#         raise Exception(f"Supplier {src.supplier} not found")

#     if src.project and src.project not in projects:
#         raise Exception(f"Project {src.project} not found")

#     # ------------------------------------------------------
#     # MAP FIELDS
#     # ------------------------------------------------------
#     doc.company = src.company
#     doc.supplier = src.supplier
#     doc.transaction_date = src.transaction_date
#     doc.schedule_date = src.schedule_date
#     doc.project = src.project
#     doc.currency = src.currency
#     doc.buying_price_list = src.buying_price_list
#     doc.conversion_rate = src.conversion_rate

#     if hasattr(doc, "remote_id"):
#         doc.remote_id = src.remote_id

#     if hasattr(doc, "source_site") and src.get("source_site"):
#         doc.source_site = src.source_site

#     # ------------------------------------------------------
#     # ITEMS
#     # ------------------------------------------------------
#     doc.set("items", [])

#     for row in src.items:

#         if row.cost_center and row.cost_center not in cost_centers:
#             raise Exception(f"Cost Center {row.cost_center} not found")

#         doc.append("items", {
#             "item_code": row.item_code,
#             "item_name": row.item_name,
#             "uom": row.uom,

#             # ✅ REQUIRED FIELDS (FIXED)
#             "conversion_factor": row.conversion_factor or 1,
#             "qty": row.qty,
#             "rate": row.rate,
#             "base_rate": row.base_rate or row.rate,
#             "base_amount": row.base_amount or (row.qty * row.rate),

#             "warehouse": row.warehouse,
#             "project": row.project,
#             "cost_center": row.cost_center,
#             "schedule_date": row.schedule_date
#         })

#     # ------------------------------------------------------
#     # FLAGS
#     # ------------------------------------------------------
#     doc.flags.ignore_permissions = True
#     doc.flags.ignore_validate = True
#     doc.flags.ignore_links = True
#     doc.flags.ignore_pricing_rule = True

#     # ------------------------------------------------------
#     # SAVE
#     # ------------------------------------------------------
#     if existing_name:
#         doc.save()
#     else:
#         doc.insert()

#     return doc


# # ==========================================================
# # DOCSTATUS SYNC
# # ==========================================================

# def sync_docstatus(doc, target_status):

#     current = doc.docstatus

#     if target_status == 0:
#         if current in (1, 2):
#             frappe.throw(f"Cannot revert PO {doc.name} to Draft")

#     elif target_status == 1:
#         if current == 0:
#             doc.submit()
#         elif current == 2:
#             frappe.throw(f"Cannot resubmit cancelled PO {doc.name}")

#     elif target_status == 2:
#         if current == 0:
#             doc.submit()
#             doc.cancel()
#         elif current == 1:
#             doc.cancel()




#########################################################################################################################
import frappe
import json
from frappe.utils import flt, nowdate


# ==========================================================
# HELPERS
# ==========================================================

def sync_docstatus(doc, target_status):
    """
    Forcefully sets docstatus to match the source using direct DB writes.

    Why not doc.submit() / doc.cancel()?
    ERPNext's on_submit hook calls update_blanket_order() which does
    frappe.get_doc("Blanket Order", ...) — if that Blanket Order doesn't
    exist in the target company it raises DoesNotExistError and the whole
    sync fails. Bypassing hooks entirely avoids this and any other
    side-effect hooks (update_reserved_qty, update_status, etc.) that
    are irrelevant for a mirror sync.
    """
    target = int(target_status)
    if doc.docstatus == target:
        return

    _force_docstatus_db(doc.name, doc.doctype, target)
    doc.docstatus = target  # keep in-memory object consistent


def _force_docstatus_db(name, doctype, target_docstatus):
    """
    Directly updates docstatus on the parent doc and ALL child rows
    without triggering any Frappe/ERPNext hooks.
    """
    # Update parent
    frappe.db.set_value(doctype, name, "docstatus", target_docstatus, update_modified=False)

    # Update every child table row that belongs to this document
    child_meta = frappe.get_meta(doctype)
    for df in child_meta.get_table_fields():
        child_doctype = df.options
        if frappe.db.table_exists(f"tab{child_doctype}"):
            frappe.db.sql(
                f"""UPDATE `tab{child_doctype}`
                    SET docstatus = %s
                    WHERE parent = %s AND parenttype = %s""",
                (target_docstatus, name, doctype)
            )

    frappe.db.commit()


def resolve_account(account_head):
    """
    Resolve an account head to a valid account name in this company.
    Tries exact match first, then matches by the numeric code prefix (before the first space).
    """
    if not account_head:
        return None

    if frappe.db.exists("Account", account_head):
        return account_head

    # Extract the numeric code before the first space, e.g. "1156101" from
    # "1156101 - Value Added Tax 15% Paid - IMC"
    parts = account_head.split(" ")
    code = parts[0].strip()

    if code:
        # Search for any account whose name starts with that code
        found = frappe.db.get_value(
            "Account",
            {"account_number": code},
            "name"
        )
        if found:
            return found

        # Fallback: LIKE search on name
        found = frappe.db.sql(
            "SELECT name FROM `tabAccount` WHERE name LIKE %s LIMIT 1",
            (f"{code} -%",),
            as_dict=False
        )
        if found:
            return found[0][0]

    return None


def upsert_purchase_order(src, existing_name):
    if existing_name:
        doc = frappe.get_doc("Purchase Order", existing_name)
        # Reopen if submitted/cancelled so we can edit.
        # Use direct DB write — not doc.cancel()/amend() — to avoid hooks.
        if doc.docstatus in (1, 2):
            frappe.db.set_value("Purchase Order", doc.name, "docstatus", 0, update_modified=False)
            frappe.db.sql(
                """UPDATE `tabPurchase Order Item`
                   SET docstatus = 0
                   WHERE parent = %s""",
                (doc.name,)
            )
            frappe.db.sql(
                """UPDATE `tabPurchase Taxes and Charges`
                   SET docstatus = 0
                   WHERE parent = %s AND parenttype = 'Purchase Order'""",
                (doc.name,)
            )
            frappe.db.commit()
            doc.docstatus = 0
        doc.set("items", [])
        doc.set("taxes", [])
        doc.set("payment_schedule", [])
    else:
        doc = frappe.new_doc("Purchase Order")
        doc.name = src.remote_id
        doc.flags.name_set = True

    # ----------------------------------------------------------
    # 1. Map Header
    # ----------------------------------------------------------
    buying_price_list = None
    if src.buying_price_list and frappe.db.exists("Price List", src.buying_price_list):
        buying_price_list = src.buying_price_list

    doc.update({
        "company":              src.company,
        "supplier":             src.supplier,
        "supplier_name":        src.supplier_name,
        "transaction_date":     src.transaction_date or nowdate(),
        "schedule_date":        src.schedule_date or src.transaction_date or nowdate(),
        "currency":             src.currency or "SAR",
        "conversion_rate":      flt(src.conversion_rate) or 1.0,
        "plc_conversion_rate":  flt(src.plc_conversion_rate) or 1.0,
        "remote_id":            src.remote_id,
        "buying_price_list":    buying_price_list,
        "apply_discount_on":    src.apply_discount_on or "Net Total",
        "disable_rounded_total": src.disable_rounded_total or 0,
        "taxes_and_charges":    None,   # avoid auto-fetch overwriting taxes table
    })

    # ----------------------------------------------------------
    # 2. Map Items — set ALL amount fields explicitly
    # ----------------------------------------------------------
    seen_items = set()
    conversion_rate = flt(src.conversion_rate) or 1.0

    for row in (src.items or []):
        fingerprint = (row.item_code, flt(row.qty), flt(row.rate))
        if fingerprint in seen_items:
            continue
        seen_items.add(fingerprint)

        qty    = flt(row.qty)
        rate   = flt(row.rate)
        amount = flt(row.amount) if flt(row.amount) else qty * rate

        doc.append("items", {
            "item_code":            row.item_code,
            "item_name":            row.item_name or row.item_code,
            "description":          getattr(row, "description", None) or row.item_name or row.item_code,
            "qty":                  qty,
            "stock_qty":            qty,
            "rate":                 rate,
            "amount":               amount,
            "net_rate":             flt(row.net_rate) if flt(getattr(row, "net_rate", 0)) else rate,
            "net_amount":           flt(row.net_amount) if flt(getattr(row, "net_amount", 0)) else amount,
            "base_rate":            rate * conversion_rate,
            "base_amount":          amount * conversion_rate,
            "base_net_rate":        (flt(row.net_rate) or rate) * conversion_rate,
            "base_net_amount":      (flt(row.net_amount) or amount) * conversion_rate,
            "uom":                  row.uom or "Nos",
            "stock_uom":            getattr(row, "stock_uom", None) or row.uom or "Nos",
            "conversion_factor":    1.0,
            "warehouse":            getattr(row, "warehouse", None),
            "schedule_date":        row.schedule_date or doc.schedule_date,
            "project":              getattr(row, "project", None) or getattr(src, "project", None),
            "cost_center":          getattr(row, "cost_center", None),
            "price_list_rate":      flt(getattr(row, "price_list_rate", 0)),
            "base_price_list_rate": flt(getattr(row, "base_price_list_rate", 0)),
            "discount_percentage":  flt(getattr(row, "discount_percentage", 0)),
            "discount_amount":      flt(getattr(row, "discount_amount", 0)),
            "item_tax_rate":        getattr(row, "item_tax_rate", "{}") or "{}",
            "against_blanket_order": 0,
            "blanket_order":        None,
            "blanket_order_rate":   0,
            "apply_tds":            getattr(row, "apply_tds", 0),
        })

    # ----------------------------------------------------------
    # 3. Map Taxes — resolve account head robustly
    # ----------------------------------------------------------
    for tax in (src.taxes or []):
        acc = resolve_account(tax.account_head)

        if not acc:
            frappe.log_error(
                f"Tax account not found: {tax.account_head} — skipping row",
                "PO Sync: Tax Account Missing"
            )
            continue

        tax_amount      = flt(tax.tax_amount)
        base_tax_amount = tax_amount * conversion_rate

        doc.append("taxes", {
            "charge_type":                          tax.charge_type or "On Net Total",
            "account_head":                         acc,
            "description":                          tax.description or acc,
            "rate":                                 flt(tax.rate),
            "tax_amount":                           tax_amount,
            "tax_amount_after_discount_amount":     flt(getattr(tax, "tax_amount_after_discount_amount", 0)) or tax_amount,
            "base_tax_amount":                      base_tax_amount,
            "base_tax_amount_after_discount_amount": base_tax_amount,
            "total":                                flt(getattr(tax, "total", 0)),
            "base_total":                           flt(getattr(tax, "base_total", 0)),
            "add_deduct_tax":                       tax.add_deduct_tax or "Add",
            "category":                             tax.category or "Total",
            "included_in_print_rate":               getattr(tax, "included_in_print_rate", 0),
            "included_in_paid_amount":              getattr(tax, "included_in_paid_amount", 0),
            "item_wise_tax_detail":                 getattr(tax, "item_wise_tax_detail", "{}") or "{}",
            "cost_center":                          getattr(tax, "cost_center", None),
        })

    # ----------------------------------------------------------
    # 4. Set flags — skip auto-recalculation
    # ----------------------------------------------------------
    doc.flags.ignore_permissions    = True
    doc.flags.ignore_validate       = True
    doc.flags.ignore_links          = True
    doc.flags.ignore_pricing_rule   = True
    doc.flags.ignore_mandatory      = True

    # ----------------------------------------------------------
    # 5. Force all totals to exactly match the source
    # ----------------------------------------------------------
    doc.total                           = flt(src.total)
    doc.net_total                       = flt(src.net_total) or flt(src.total)
    doc.base_total                      = flt(src.base_total)
    doc.base_net_total                  = flt(src.base_net_total) or flt(src.base_total)
    doc.total_qty                       = flt(src.total_qty)
    doc.grand_total                     = flt(src.grand_total)
    doc.base_grand_total                = flt(src.base_grand_total)
    doc.base_taxes_and_charges_added    = flt(src.base_taxes_and_charges_added)
    doc.taxes_and_charges_added         = flt(src.taxes_and_charges_added)
    doc.base_taxes_and_charges_deducted = flt(src.base_taxes_and_charges_deducted)
    doc.taxes_and_charges_deducted      = flt(src.taxes_and_charges_deducted)
    doc.total_taxes_and_charges         = flt(src.total_taxes_and_charges)
    doc.base_total_taxes_and_charges    = flt(src.base_total_taxes_and_charges)
    doc.tax_withholding_net_total       = flt(getattr(src, "tax_withholding_net_total", 0))
    doc.base_tax_withholding_net_total  = flt(getattr(src, "base_tax_withholding_net_total", 0))
    doc.rounding_adjustment             = flt(src.rounding_adjustment)
    doc.base_rounding_adjustment        = flt(src.base_rounding_adjustment)
    doc.rounded_total                   = flt(src.rounded_total)
    doc.base_rounded_total              = flt(src.base_rounded_total)
    doc.in_words                        = getattr(src, "in_words", "")
    doc.base_in_words                   = getattr(src, "base_in_words", "")

    # ----------------------------------------------------------
    # 6. Save (as Draft)
    # ----------------------------------------------------------
    doc.save()
    frappe.db.commit()

    # ----------------------------------------------------------
    # 7. Directly write taxes to DB in case ignore_validate
    #    caused them to be skipped by the ORM
    # ----------------------------------------------------------
    _ensure_taxes_in_db(doc)

    return doc


def _ensure_taxes_in_db(doc):
    """
    After save(), verify that tax rows actually landed in the DB.
    If not, insert them directly. This guards against Frappe versions
    that silently drop child rows when ignore_validate=True.
    """
    existing_count = frappe.db.count(
        "Purchase Taxes and Charges",
        {"parent": doc.name, "parenttype": "Purchase Order"}
    )

    if existing_count >= len(doc.taxes):
        return  # All rows are present, nothing to do

    # Wipe and re-insert
    frappe.db.delete("Purchase Taxes and Charges", {
        "parent": doc.name,
        "parenttype": "Purchase Order"
    })

    for idx, tax in enumerate(doc.taxes, start=1):
        frappe.db.insert({
            "doctype":                              "Purchase Taxes and Charges",
            "name":                                 frappe.generate_hash(length=10),
            "parent":                               doc.name,
            "parentfield":                          "taxes",
            "parenttype":                           "Purchase Order",
            "owner":                                frappe.session.user,
            "modified_by":                          frappe.session.user,
            "docstatus":                            0,
            "idx":                                  idx,
            "charge_type":                          tax.charge_type,
            "account_head":                         tax.account_head,
            "description":                          tax.description,
            "rate":                                 tax.rate,
            "tax_amount":                           tax.tax_amount,
            "tax_amount_after_discount_amount":     tax.tax_amount_after_discount_amount,
            "base_tax_amount":                      tax.base_tax_amount,
            "base_tax_amount_after_discount_amount": tax.base_tax_amount_after_discount_amount,
            "total":                                tax.total,
            "base_total":                           tax.base_total,
            "add_deduct_tax":                       tax.add_deduct_tax,
            "category":                             tax.category,
            "included_in_print_rate":               tax.included_in_print_rate,
            "included_in_paid_amount":              tax.included_in_paid_amount,
            "item_wise_tax_detail":                 tax.item_wise_tax_detail,
            "cost_center":                          tax.cost_center,
        })

    frappe.db.commit()


# ==========================================================
# ENTRY POINTS
# ==========================================================

@frappe.whitelist()
def sync_purchase_order_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)
    frappe.enqueue(
        "emkan_insights.emkan_insights.external_po_sync.sync_bulk_purchase_orders",
        queue="long",
        names=names,
        timeout=2000
    )
    return "Sync Queued"


def sync_bulk_purchase_orders(names):
    success, failed = 0, 0
    for name in names:
        try:
            src      = frappe.get_doc("External Purchase Order", name)
            existing = src.remote_id if frappe.db.exists("Purchase Order", src.remote_id) else None

            doc = upsert_purchase_order(src, existing)
            sync_docstatus(doc, src.docstatus)

            success += 1
            if success % 10 == 0:
                frappe.db.commit()

        except Exception:
            failed += 1
            frappe.log_error(f"Sync Fail: {name}", frappe.get_traceback())

    frappe.db.commit()
    return {"success": success, "failed": failed}