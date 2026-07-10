# import frappe
# import json

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
# def sync_external_sales_invoice_docs(source_doctype, names):

#     if isinstance(names, str):
#         names = json.loads(names)

#     results = []

#     for name in names:
#         try:
#             ext_si = frappe.get_doc(source_doctype, name)

#             # ------------------------------------------------
#             # CHECK EXISTING
#             # ------------------------------------------------
#             existing_si = frappe.db.get_value(
#                 "Sales Invoice",
#                 {"remote_id": ext_si.name},
#                 "name"
#             )

#             if existing_si:
#                 results.append({
#                     "name": existing_si,
#                     "status": "exists"
#                 })
#                 continue

#             si = frappe.new_doc("Sales Invoice")

#             si.remote_id = ext_si.name

#             if hasattr(si, "source_site"):
#                 si.source_site = ext_si.source_site

#             # ------------------------------------------------
#             # COPY MAIN FIELDS
#             # ------------------------------------------------
#             for field, value in ext_si.as_dict().items():

#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "taxes", "remote_id"]
#                     and hasattr(si, field)
#                 ):
#                     si.set(field, value)

#             # ------------------------------------------------
#             # ITEMS
#             # ------------------------------------------------
    
#                 si.set("items", [])

#                 for row in ext_si.items:

#                     item_row = {}

#                     for field, value in row.as_dict().items():

#                         if (
#                             field not in SYSTEM_FIELDS
#                             and field not in IGNORE_ITEM_FIELDS
#                         ):
#                             item_row[field] = value

#                     # Remove invalid Sales Order link
#                     if row.get("sales_order") and not frappe.db.exists("Sales Order", row.sales_order):
#                         item_row["sales_order"] = None
#                         item_row["so_detail"] = None

#                     # Remove invalid Delivery Note link
#                     if row.get("delivery_note") and not frappe.db.exists("Delivery Note", row.delivery_note):
#                         item_row["delivery_note"] = None
#                         item_row["dn_detail"] = None

#                     if not item_row.get("warehouse"):
#                         item_row["warehouse"] = DEFAULT_WAREHOUSE

#                     si.append("items", item_row)

#             # ------------------------------------------------
#             # TAXES
#             # ------------------------------------------------
#             if hasattr(ext_si, "taxes"):

#                 si.set("taxes", [])

#                 for row in ext_si.taxes:

#                     tax_row = {}

#                     for field, value in row.as_dict().items():

#                         if field not in SYSTEM_FIELDS:
#                             tax_row[field] = value

#                     si.append("taxes", tax_row)

#             # ------------------------------------------------
#             # FLAGS
#             # ------------------------------------------------
#             si.flags.ignore_permissions = True
#             si.flags.ignore_validate = True
#             si.flags.ignore_mandatory = True
#             si.flags.ignore_links = True

#             # ------------------------------------------------
#             # INSERT
#             # ------------------------------------------------
#             si.insert(
#                 ignore_permissions=True,
#                 ignore_links=True,
#                 ignore_mandatory=True
#             )

#             # ------------------------------------------------
#             # FORCE SAME NAME AS EXTERNAL
#             # ------------------------------------------------
#             if si.name != ext_si.name:

#                 frappe.db.sql("""
#                     UPDATE `tabSales Invoice`
#                     SET name = %s
#                     WHERE name = %s
#                 """, (ext_si.name, si.name))

#                 for child_table in [
#                     "Sales Invoice Item",
#                     "Sales Taxes and Charges",
#                     "Payment Schedule"
#                 ]:
#                     frappe.db.sql("""
#                         UPDATE `tab{0}`
#                         SET parent = %s
#                         WHERE parent = %s
#                     """.format(child_table), (ext_si.name, si.name))

#                 frappe.db.commit()

#                 si.name = ext_si.name

#             # ------------------------------------------------
#             # DOCSTATUS SYNC
#             # ------------------------------------------------
#             if ext_si.docstatus == 1:
#                 si.submit()

#             elif ext_si.docstatus == 2:
#                 si.submit()
#                 si.cancel()

#             results.append({
#                 "name": si.name,
#                 "status": "synced"
#             })

#         except Exception:

#             frappe.log_error(
#                 f"Sales Invoice Sync Error: {name}",
#                 frappe.get_traceback()
#             )

#             results.append({
#                 "name": name,
#                 "status": "failed"
#             })

#     return results

import frappe
import json
from frappe.utils import flt

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation",
    "quotation_item",
    "prevdoc_doctype",
    "prevdoc_docname",
    "so_detail",
    "against_sales_order",
    "name"
}

# Fields resolved explicitly further down — don't let the generic field-copy
# loop blindly overwrite them with unresolved raw values from the source doc.
EXPLICITLY_HANDLED_HEADER_FIELDS = {
    "items", "taxes", "payment_schedule", "remote_id", "custom_remote_id",
    "customer"
}


# ==========================================================
# DUPLICATE CLEANUP (kept as a post-insert safety net)
# ==========================================================

def clean_duplicate_si_items(parent_name):
    """
    Belt-and-suspenders cleanup: if two item rows still ended up identical
    in every tracked field (including custom_remote_id — meaning they trace
    back to the exact same source row), remove the extra one. The real fix
    is deduping before appending (see _dedupe_source_items below); this stays
    as a safety net in case anything slips through.
    """
    frappe.db.sql("""
        DELETE t1 FROM `tabSales Invoice Item` t1
        INNER JOIN `tabSales Invoice Item` t2
        WHERE
            t1.name > t2.name
            AND t1.parent = t2.parent
            AND t1.parent = %s
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
            AND IFNULL(t1.delivery_note, '') = IFNULL(t2.delivery_note, '')
            AND IFNULL(t1.sales_order, '') = IFNULL(t2.sales_order, '')
            AND IFNULL(t1.custom_remote_id, '') = IFNULL(t2.custom_remote_id, '')
    """, (parent_name,))


def _dedupe_source_items(ext_items):
    """
    Prevent duplicate rows from ever being appended in the first place,
    instead of relying only on post-insert cleanup. If the source pull
    (pagination overlap, retry, etc.) handed back the same underlying row
    more than once — identified by its own unique row name — keep only the
    first occurrence.
    """
    seen = set()
    deduped = []
    for row in ext_items:
        row_id = row.name
        if row_id in seen:
            continue
        seen.add(row_id)
        deduped.append(row)
    return deduped


# ==========================================================
# LINK RESOLVERS
# ==========================================================

def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    """
    Three-tier resolution:
    1. Prefixed local name: {company_abbr}-{remote_name}
    2. Remote ID lookup: {remote_id_field: remote_name}
    3. Raw name existence check
    """
    if not remote_or_local_name:
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    meta = frappe.get_meta(doctype)
    for remote_field in ("custom_remote_id", "remote_id"):
        if meta.has_field(remote_field):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def get_child_detail(doctype, parent, item_code):
    if not parent:
        return None
    return frappe.db.get_value(
        doctype,
        {"parent": parent, "item_code": item_code},
        "name"
    )


def _resolve_customer(customer_ref, company_abbr):
    """
    Resolve the customer explicitly using the same 3-tier pattern used for
    Sales Order / Delivery Note / Purchase Order links below, with a log
    (not a hard failure) if it can't be resolved — leave blank and let
    ignore_mandatory cover it, rather than dropping the whole invoice.
    """
    if not customer_ref:
        return None

    resolved = resolve_local_doc("Customer", customer_ref, company_abbr)
    if not resolved:
        frappe.log_error(
            title=f"Sales Invoice Sync - Customer '{customer_ref}' unresolved",
            message="Customer could not be resolved to a local Customer. Leaving field blank so the invoice still syncs."
        )
    return resolved


def _resolve_default_warehouse(company):
    """Resolve a real warehouse for the invoice's own company."""
    if not company:
        return None
    return frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")


def _ensure_item_uom(item_code, uom):
    """
    ERPNext throws 'UOM {uom} not found in Item {item_code}' if a transaction
    row uses a UOM that isn't registered on that Item. Auto-register it
    (conversion factor 1.0) instead of letting the whole invoice fail.
    """
    if not item_code or not uom:
        return

    if not frappe.db.exists("UOM", uom):
        return

    already_registered = frappe.db.exists("UOM Conversion Detail", {
        "parent": item_code,
        "parenttype": "Item",
        "uom": uom
    })
    if already_registered:
        return

    try:
        frappe.get_doc({
            "doctype": "UOM Conversion Detail",
            "parent": item_code,
            "parenttype": "Item",
            "parentfield": "uoms",
            "uom": uom,
            "conversion_factor": 1.0
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title=f"Failed to auto-register UOM {uom} on Item {item_code}",
            message=str(e)
        )


# ==========================================================
# MAIN ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_sales_invoice_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_si = frappe.get_doc(source_doctype, name)
            remote_id = ext_si.name

            # ------------------------------------------------
            # BUILD TARGET NAME: {company_abbr}-{remote_id}
            # ------------------------------------------------
            company_abbr = frappe.db.get_value('Company', ext_si.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Sales Invoice Sync: no company abbr found for company "
                    f"'{ext_si.company}' on {remote_id}. Name will be created "
                    f"without a company prefix.",
                    "ExternalSalesInvoice Sync - missing company abbr"
                )

            # FIX: don't double-prefix. If remote_id already starts with the
            # company abbr (the external system had already stamped it on),
            # use it as-is instead of prepending the abbr a second time.
            # This is what was producing names like "IMC-IMC-CINV-25-00123".
            if company_abbr and remote_id.startswith(f"{company_abbr}-"):
                target_name = remote_id
            else:
                target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # Dynamic remote_id field detection
            # ------------------------------------------------
            remote_id_field = None
            si_meta = frappe.get_meta("Sales Invoice")
            for candidate in ("custom_remote_id", "remote_id"):
                if si_meta.has_field(candidate):
                    remote_id_field = candidate
                    break

            if not remote_id_field:
                frappe.throw(
                    "Sales Invoice has neither 'custom_remote_id' nor "
                    "'remote_id' field - cannot track sync identity."
                )

            # ------------------------------------------------
            # Check existing by dynamic remote_id field
            # ------------------------------------------------
            existing_si = frappe.db.get_value(
                "Sales Invoice",
                {remote_id_field: remote_id},
                "name"
            )

            if existing_si:
                results.append({
                    "name": existing_si,
                    "status": "exists"
                })
                continue

            si = frappe.new_doc("Sales Invoice")

            # ------------------------------------------------
            # SET NAME WITH COMPANY ABBR PREFIX
            # ------------------------------------------------
            si.set(remote_id_field, remote_id)
            si.name = target_name
            si.flags.name_set = True

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            si.flags.ignore_permissions = True
            si.flags.ignore_validate = True
            si.flags.ignore_mandatory = True
            si.flags.ignore_links = True
            si.flags.ignore_naming_series = True
            frappe.flags.ignore_stock_validation = True

            if hasattr(si, "source_site"):
                si.source_site = ext_si.source_site

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # ------------------------------------------------
            for field, value in ext_si.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in EXPLICITLY_HANDLED_HEADER_FIELDS
                    and hasattr(si, field)
                ):
                    si.set(field, value)

            # Resolve customer explicitly instead of copying it verbatim
            # through the generic loop above.
            si.customer = _resolve_customer(ext_si.get("customer"), company_abbr)

            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
            si.set("items", [])

            # Dedupe by source row identity before ever appending, instead
            # of only cleaning up duplicates after insert.
            deduped_items = _dedupe_source_items(ext_si.items)

            for row in deduped_items:

                item_row = {}

                for field, value in row.as_dict().items():

                    if (
                        field not in SYSTEM_FIELDS
                        and field not in IGNORE_ITEM_FIELDS
                    ):
                        item_row[field] = value

                # Resolve Sales Order with company abbr (3-tier)
                if row.get("sales_order"):
                    local_so = resolve_local_doc("Sales Order", row.sales_order, company_abbr)
                    item_row["sales_order"] = local_so
                    item_row["so_detail"] = get_child_detail(
                        "Sales Order Item", local_so, row.item_code
                    ) if local_so else None
                else:
                    item_row["sales_order"] = None
                    item_row["so_detail"] = None

                # Resolve Delivery Note with company abbr (3-tier)
                if row.get("delivery_note"):
                    local_dn = resolve_local_doc("Delivery Note", row.delivery_note, company_abbr)
                    item_row["delivery_note"] = local_dn
                    item_row["dn_detail"] = get_child_detail(
                        "Delivery Note Item", local_dn, row.item_code
                    ) if local_dn else None
                else:
                    item_row["delivery_note"] = None
                    item_row["dn_detail"] = None

                # Resolve Purchase Order with company abbr (3-tier)
                if row.get("purchase_order"):
                    local_po = resolve_local_doc("Purchase Order", row.purchase_order, company_abbr)
                    item_row["purchase_order"] = local_po
                    item_row["po_detail"] = get_child_detail(
                        "Purchase Order Item", local_po, row.item_code
                    ) if local_po else None
                else:
                    item_row["purchase_order"] = None
                    item_row["po_detail"] = None

                # Map external row identity to custom_remote_id
                if frappe.get_meta("Sales Invoice Item").has_field("custom_remote_id"):
                    item_row["custom_remote_id"] = row.name

                # Resolve a real per-company default warehouse.
                if not item_row.get("warehouse"):
                    item_row["warehouse"] = _resolve_default_warehouse(ext_si.company)

                # Auto-register the item's UOM if missing - avoids
                # "UOM X not found in Item Y".
                if item_row.get("item_code") and item_row.get("uom"):
                    _ensure_item_uom(item_row["item_code"], item_row["uom"])

                si.append("items", item_row)

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_si, "taxes"):

                si.set("taxes", [])

                for row in ext_si.taxes:

                    tax_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value

                    si.append("taxes", tax_row)

            # ------------------------------------------------
            # PAYMENT SCHEDULE
            # ------------------------------------------------
            if hasattr(ext_si, "payment_schedule"):

                si.set("payment_schedule", [])

                for row in ext_si.payment_schedule:

                    schedule_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            schedule_row[field] = value

                    si.append("payment_schedule", schedule_row)

            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            si.merge_similar_items = 0
            si.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # Safety net — should be a no-op now that items are deduped
            # before appending, but kept in case anything slips through.
            clean_duplicate_si_items(si.name)
            frappe.db.commit()

            # ------------------------------------------------
            # NAMING SAFETY NET
            # ------------------------------------------------
            if si.name != target_name:

                if not frappe.db.exists("Sales Invoice", target_name):

                    old_name = si.name

                    frappe.db.sql("""
                        UPDATE `tabSales Invoice`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, old_name))

                    for child_table in [
                        "Sales Invoice Item",
                        "Sales Taxes and Charges",
                        "Payment Schedule"
                    ]:
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""
                                UPDATE `tab{0}`
                                SET parent = %s
                                WHERE parent = %s
                            """.format(child_table), (target_name, old_name))

                    frappe.db.commit()
                    si.name = target_name

                else:
                    frappe.log_error(
                        f"Sales Invoice Sync: could not rename {si.name} to "
                        f"{target_name} because that name already exists.",
                        "ExternalSalesInvoice Sync - name collision"
                    )

            # ------------------------------------------------
            # DOCSTATUS SYNC
            # ------------------------------------------------
            if ext_si.docstatus == 1:
                si.submit()

            elif ext_si.docstatus == 2:
                si.submit()
                si.cancel()

            results.append({
                "name": si.name,
                "status": "synced"
            })

        except Exception as e:
            error = frappe.get_traceback()

            frappe.log_error(
                title=f"Sales Invoice Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results