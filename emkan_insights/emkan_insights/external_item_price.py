import frappe
import json
from frappe.utils import cint, flt
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.sync_guard import set_source_site_fields
from emkan_insights.emkan_insights.uom_resolver import resolve_uom_or_none

# ==========================================================
# HELPERS
# ==========================================================

def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    """
    Resolve a linked local document.
    1. Prefixed local name: {company_abbr}-{remote_name}
    2. remote_id / custom_remote_id lookup
    3. Raw name existence check
    """
    if not remote_or_local_name or not frappe.db.exists("DocType", doctype):
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


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_item_prices(names=None):

    if isinstance(names, str):
        names = json.loads(names)

    # If no names passed, sync everything
    if not names:
        names = frappe.db.sql("""
            SELECT name
            FROM `tabExternal Item Price`
            WHERE
                last_synced_on IS NULL
                OR modified >= last_synced_on
        """, pluck=True)

    # --- TEMPORARY SYNCHRONOUS TEST ---
    results = []
    for name in names:
        try:
            res = sync_single_item_price(name)
            if res == "skipped":
                results.append({"name": name, "status": "skipped (check Error Log)"})
            else:
                results.append({"name": name, "status": "synced"})
        except Exception as e:
            results.append({"name": name, "status": "failed", "error": str(e)})
    
    return results
    # ----------------------------------


# ==========================================================
# BATCH SYNC
# ==========================================================

def sync_item_price_batch(names):
    """
    Processed in background queue.
    """
    success = 0
    failed = 0
    skipped = 0

    for idx, source_name in enumerate(names, start=1):

        try:
            result = sync_single_item_price(source_name)
            if result == "skipped":
                skipped += 1
            else:
                success += 1

        except Exception:

            failed += 1

            frappe.log_error(
                title=f"External Item Price Sync Failed: {source_name}",
                message=frappe.get_traceback()
            )

    return {
        "success": success,
        "failed": failed,
        "skipped": skipped
    }


# ==========================================================
# SINGLE ITEM PRICE SYNC
# ==========================================================

def sync_single_item_price(source_name):

    src = frappe.get_doc("External Item Price", source_name)
    sync_started_at = src.modified

    target_name = src.remote_id

    existing = None

    if target_name and frappe.db.exists("Item Price", target_name):
        existing = target_name

    # ------------------------------------------------------
    # Determine Company Abbr for prefixing linked docs
    # ------------------------------------------------------
    company = getattr(src, "company", None)
    company_abbr = frappe.db.get_value("Company", company, "abbr") if company else ""

    # ------------------------------------------------------
    # LOAD / CREATE
    # ------------------------------------------------------

    if existing:
        item_price = frappe.get_doc("Item Price", existing)
    else:
        item_price = frappe.new_doc("Item Price")

        if target_name:
            item_price.name = target_name
            item_price.flags.name_set = True

    # ------------------------------------------------------
    # BASIC FIELDS
    # ------------------------------------------------------

    item_price.update({

        "custom_remote_id": src.name,

        "selling": cint(getattr(src, "selling", 1)),
        "buying": cint(getattr(src, "buying", 0)),
        "currency": getattr(src, "currency", None),
        "price_list_rate": flt(getattr(src, "price_list_rate", 0)),
        
        "valid_from": getattr(src, "valid_from", None),
        "valid_upto": getattr(src, "valid_upto", None),
        
        "batch_no": getattr(src, "batch_no", None),
        "uom": resolve_uom_or_none(getattr(src, "uom", None)),
        "packing_unit": flt(getattr(src, "packing_unit", 0)),
        
        "warehouse": getattr(src, "warehouse", None),
        "reference": getattr(src, "reference", None),
        
        "margin_type": getattr(src, "margin_type", None),
        "margin_rate_or_amount": flt(getattr(src, "margin_rate_or_amount", 0)),
        "discount_percentage": flt(getattr(src, "discount_percentage", 0)),
        "discount_amount": flt(getattr(src, "discount_amount", 0)),

    })
    set_source_site_fields(item_price, site_url=getattr(src, "source_site", None))

    # ------------------------------------------------------
    # RESOLVE LINKED FIELDS (Company Prefix Fix)
    # ------------------------------------------------------
    
    # 1. Resolve Item Code (MANDATORY - Skip if missing)
    raw_item_code = getattr(src, "item_code", None)
    local_item_code = None
    
    if raw_item_code:
        local_item_code = resolve_local_doc("Item", raw_item_code, company_abbr)
        # Final fallback to raw name if resolution fails
        if not local_item_code and frappe.db.exists("Item", raw_item_code):
            local_item_code = raw_item_code

    if not local_item_code:
        frappe.log_error(
            title=f"Item Price Sync Skipped: Missing Item '{raw_item_code}'",
            message=f"Cannot sync External Item Price '{source_name}' because the Item '{raw_item_code}' does not exist locally. Please sync the Item master first."
        )
        return "skipped"  # Gracefully skip, don't throw error

    item_price.item_code = local_item_code

    # 2. Resolve Price List (MANDATORY - Skip if missing)
    raw_price_list = getattr(src, "price_list", None)
    local_price_list = None
    
    if raw_price_list:
        local_price_list = resolve_local_doc("Price List", raw_price_list, company_abbr)
        if not local_price_list and frappe.db.exists("Price List", raw_price_list):
            local_price_list = raw_price_list

    if not local_price_list:
        frappe.log_error(
            title=f"Item Price Sync Skipped: Missing Price List '{raw_price_list}'",
            message=f"Cannot sync External Item Price '{source_name}' because the Price List '{raw_price_list}' does not exist locally."
        )
        return "skipped"  # Gracefully skip

    item_price.price_list = local_price_list

    # 3. Resolve Customer (Optional - clear if missing)
    raw_customer = getattr(src, "customer", None)
    if raw_customer:
        item_price.customer = resolve_local_doc("Customer", raw_customer, company_abbr)

    # 4. Resolve Supplier (Optional - clear if missing)
    raw_supplier = getattr(src, "supplier", None)
    if raw_supplier:
        item_price.supplier = resolve_local_doc("Supplier", raw_supplier, company_abbr)

    # ------------------------------------------------------
    # FLAGS & SAVE
    # ------------------------------------------------------

    item_price.flags.ignore_permissions = True

    item_price.save()

    src.db_set(
        "last_synced_on",
        sync_started_at,
        update_modified=False
    )

    return item_price.name
