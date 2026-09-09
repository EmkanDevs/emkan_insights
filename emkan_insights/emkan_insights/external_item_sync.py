import frappe
import json
from frappe.utils import cint, flt
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import (
    active_item_during_opening_stock,
    set_source_site_fields,
)
from emkan_insights.emkan_insights.uom_resolver import resolve_uom, resolve_uom_or_none

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


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


def _build_company_prefixed_name(company_abbr, name):
    if not name:
        return None

    name = str(name).strip()
    if not name or not company_abbr:
        return name

    prefix = f"{company_abbr}-"
    if name.startswith(prefix):
        return name

    return f"{prefix}{name}"


# ==========================================================
# ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_items(names=None, sync_now=False):
    """
    Dedicated bulk sync for External Item -> Item

    Handles:
    - de-duplication
    - chunking
    - queueing
    - timeout prevention
    - large datasets
    """

    if isinstance(names, str):
        names = json.loads(names)

    # If no names passed, sync everything unsynced
    if not names:
        names = frappe.db.sql("""
            SELECT name
            FROM `tabExternal Item`
            WHERE
                last_synced_on IS NULL
                OR modified >= last_synced_on
        """, pluck=True)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: sync_item_batch(names=chunk),
        )

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"item_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_item_sync.sync_item_batch",
            queue="long",
            job_name=job_name,
            timeout=5000,
            names=chunk,
        )
        queued_chunks += 1

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BATCH SYNC
# ==========================================================

def sync_item_batch(names):
    """
    Processed in background queue.
    
    All database writes (item.save()) happen within a single transaction.
    Frappe automatically commits the transaction when the background job finishes.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return {"success": 0, "failed": 0}

    success = 0
    failed = 0

    for idx, source_name in enumerate(names, start=1):

        try:
            sync_single_item(source_name)
            success += 1

            # NO periodic commit — the single transaction handles it safely

        except Exception as e:
            log_record_failure("Item", source_name, str(e))
            raise

    return {
        "success": success,
        "failed": failed
    }


# ==========================================================
# SINGLE ITEM SYNC
# ==========================================================

def sync_single_item(source_name):

    src = frappe.get_doc("External Item", source_name)
    sync_started_at = src.modified

    company = getattr(src, "company", None)
    company_abbr = frappe.db.get_value("Company", company, "abbr") if company else ""
    target_name = _build_company_prefixed_name(company_abbr, src.remote_id or src.name)

    existing = None

    if target_name and frappe.db.exists("Item", target_name):
        existing = target_name

    # ------------------------------------------------------
    # LOAD / CREATE
    # ------------------------------------------------------

    if existing:
        item = frappe.get_doc("Item", existing)

        # Skip only when updating an already-synced Item that is newer than the
        # source External Item. Do NOT apply this to new Items: a fresh doc's
        # `modified` is empty, and get_datetime(None) returns "now", which is
        # always newer than src.modified — that would silently skip every item
        # that does not already exist locally (e.g. FA-4020054).
        if frappe.utils.get_datetime(item.modified) > frappe.utils.get_datetime(sync_started_at):
            return
    else:
        item = frappe.new_doc("Item")
        if target_name:
            item.name = target_name
            item.flags.name_set = True
    # ------------------------------------------------------
    # BASIC FIELDS
    # ------------------------------------------------------
    item_group = resolve_local_doc(
        "Item Group",
        getattr(src, "item_group", None) or "All Item Groups",
        company_abbr,
    ) or getattr(src, "item_group", None) or "All Item Groups"

    item.update({

        "custom_remote_id": src.name,

        "item_code": target_name,
        "item_name": getattr(src, "item_name", None) or getattr(src, "item_code", None) or src.name,
        "item_group": item_group,
        "stock_uom": resolve_uom(getattr(src, "stock_uom", None) or "Nos"),
        "description": getattr(src, "description", None),

        "disabled": cint(getattr(src, "disabled", 0)),
        "is_stock_item": cint(getattr(src, "is_stock_item", 0)),
        "brand": getattr(src, "brand", None),

        "opening_stock": flt(getattr(src, "opening_stock", 0)),
        "valuation_rate": flt(getattr(src, "valuation_rate", 0)),
        "standard_rate": flt(getattr(src, "standard_selling_rate", 0)),

        "is_fixed_asset": cint(getattr(src, "is_fixed_asset", 0)),
        "auto_create_assets": cint(getattr(src, "auto_create_assets_on_purchase", 0)),
        "asset_category": getattr(src, "asset_category", None),
        "asset_naming_series": getattr(src, "asset_naming_series", None),

        "has_batch_no": cint(getattr(src, "has_batch_no", 0)),
        "create_new_batch": cint(getattr(src, "automatically_create_new_batch", 0)),
        "batch_number_series": getattr(src, "batch_number_series", None),
        "has_expiry_date": cint(getattr(src, "has_expiry_date", 0)),
        "retain_sample": cint(getattr(src, "retain_sample", 0)),
        "sample_quantity": flt(getattr(src, "max_sample_quantity", 0)),

        "has_serial_no": cint(getattr(src, "has_serial_no", 0)),
        "serial_number_series": getattr(src, "serial_number_series", None),

        "is_purchase_item": cint(getattr(src, "allow_purchase", 1)),
        "is_sales_item": cint(getattr(src, "allow_sales", 1)),
        "lead_time_days": cint(getattr(src, "lead_time_in_days", 0)),
        "last_purchase_rate": flt(getattr(src, "last_purchase_rate", 0)),
        "max_discount": flt(getattr(src, "max_discount", 0)),

        "country_of_origin": getattr(src, "country_of_origin", None),
        "customs_tariff_number": getattr(src, "customs_tariff_number", None),

        "default_material_request_type": getattr(src, "default_material_request_type", None),
        "valuation_method": getattr(src, "valuation_method", None),
        "shelf_life_in_days": cint(getattr(src, "shelf_life_in_days", 0)),
        "end_of_life": getattr(src, "end_of_life", None),

        "weight_per_unit": flt(getattr(src, "weight_per_unit", 0)),
        "weight_uom": resolve_uom_or_none(getattr(src, "weight_uom", None)),
        "warranty_period": cint(getattr(src, "warranty_period_in_days", 0)),

        "allow_negative_stock": cint(getattr(src, "allow_negative_stock", 0)),
        "over_delivery_receipt_allowance": flt(getattr(src, "over_delivery_receipt_allowance", 0)),
        "over_billing_allowance": flt(getattr(src, "over_billing_allowance", 0)),

        "min_order_qty": flt(getattr(src, "minimum_order_qty", 0)),
        "safety_stock": flt(getattr(src, "safety_stock", 0)),

        "inspection_required_before_purchase": cint(getattr(src, "inspection_required_before_purchase", 0)),
        "inspection_required_before_delivery": cint(getattr(src, "inspection_required_before_delivery", 0)),
        "include_item_in_manufacturing": cint(getattr(src, "include_item_in_manufacturing", 0)),
        "supply_raw_materials": cint(getattr(src, "supply_raw_materials_for_purchase", 0)),

        "is_customer_provided_item": cint(getattr(src, "is_customer_provided_item", 0)),
        "delivered_by_supplier": cint(getattr(src, "delivered_by_supplier_drop_ship", 0)),
        "customer": getattr(src, "customer", None),

        # "default_bom" removed from here to be resolved explicitly below

        "grant_commission": cint(getattr(src, "grant_commission", 1)),

        "variant_of": getattr(src, "variant_of", None),
        "variant_based_on": getattr(src, "variant_based_on", None),

    })
    set_source_site_fields(item, site_url=getattr(src, "source_site", None))

    # ------------------------------------------------------
    # RESOLVE DEFAULT BOM (Company Prefix Fix)
    # ------------------------------------------------------
    raw_bom = getattr(src, "default_bom", None)
    if raw_bom:
        item.default_bom = resolve_local_doc("BOM", raw_bom, company_abbr)

    # ------------------------------------------------------
    # HELPER: SYNC CHILD TABLES
    # ------------------------------------------------------

    def sync_child_table(target_field, source_field, field_map):
        """Clear target child table and rebuild from External Item"""
        item.set(target_field, [])

        if not hasattr(src, source_field):
            return

        rows = getattr(src, source_field, [])
        for row in rows:
            new_row = {}
            for target_key, source_spec in field_map.items():
                linked_doctype = None
                source_key = source_spec
                if isinstance(source_spec, tuple):
                    source_key, linked_doctype = source_spec

                val = getattr(row, source_key, None)
                if val is not None:
                    if linked_doctype:
                        val = resolve_local_doc(linked_doctype, val, company_abbr) or val
                    new_row[target_key] = val
            if new_row:
                item.append(target_field, new_row)

    # ------------------------------------------------------
    # 1. UOMs
    # ------------------------------------------------------

    # Rebuild the UOM table from the source, deduplicating by UOM. The source
    # External Item may list the same UOM more than once (e.g. two rows with
    # "m3"), which ERPNext's Item.validate_conversion_factor() rejects with
    # "Unit of Measure X has been entered more than once". Keep the first
    # occurrence of each UOM.
    item.set("uoms", [])
    seen_uoms = set()
    for row in getattr(src, "uoms", []) or []:
        uom_value = getattr(row, "uom", None)
        if not uom_value:
            continue
        uom_value = resolve_uom(uom_value)
        if uom_value in seen_uoms:
            continue
        seen_uoms.add(uom_value)
        item.append("uoms", {
            "uom": uom_value,
            "conversion_factor": flt(getattr(row, "conversion_factor", 1)) or 1,
        })

    # ERPNext requires the Stock UOM to exist in the UOM table with factor 1.0
    stock_uom_exists = any(
        d.uom == item.stock_uom for d in item.get("uoms", [])
    )
    if not stock_uom_exists:
        item.append("uoms", {
            "uom": item.stock_uom,
            "conversion_factor": 1.0
        })

    # ------------------------------------------------------
    # 2. ITEM DEFAULTS
    # ------------------------------------------------------

    sync_child_table("item_defaults", "item_defaults", {
        "company": "company",
        "default_warehouse": "default_warehouse",
        "default_price_list": "default_price_list",
        "buying_cost_center": "default_buying_cost_center",
        "selling_cost_center": "default_selling_cost_center",
        "expense_account": "default_expense_account",
        "income_account": "default_income_account",
        "default_supplier": "default_supplier",
        "deferred_expense_account": "deferred_expense_account",
        "deferred_revenue_account": "deferred_revenue_account",
        "default_discount_account": "default_discount_account",
        "default_provisional_account": "default_provisional_account",
    })

    # ------------------------------------------------------
    # 3. BARCODES
    # ------------------------------------------------------

    sync_child_table("barcodes", "barcodes", {
        "barcode": "barcode",
        "barcode_type": "barcode_type",
        "uom": "uom"
    })

    # ------------------------------------------------------
    # 4. REORDER LEVELS
    # ------------------------------------------------------

    sync_child_table("reorder_levels", "reorder_levels", {
        "warehouse": ("warehouse", "Warehouse"),
        "warehouse_reorder_level": "re_order_level",
        "warehouse_reorder_qty": "re_order_qty",
        "material_request_type": "material_request_type"
    })

    # ------------------------------------------------------
    # 5. SUPPLIER ITEMS
    # ------------------------------------------------------

    sync_child_table("supplier_items", "supplier_items", {
        "supplier": "supplier",
        "supplier_part_no": "supplier_part_number"
    })

    # ------------------------------------------------------
    # 6. CUSTOMER ITEMS
    # ------------------------------------------------------

    sync_child_table("customer_items", "customer_items", {
        "customer_name": "customer_name",
        "customer_group": ("customer_group", "Customer Group"),
        "ref_code": "ref_code"
    })

    # ------------------------------------------------------
    # 7. TAXES
    # ------------------------------------------------------

    sync_child_table("taxes", "taxes", {
        "item_tax_template": "item_tax_template",
        "tax_category": "tax_category",
        "valid_from": "valid_from",
        "minimum_net_rate": "minimum_net_rate",
        "maximum_net_rate": "maximum_net_rate"
    })

    # ------------------------------------------------------
    # 8. VARIANT ATTRIBUTES
    # ------------------------------------------------------

    sync_child_table("attributes", "attributes", {
        "attribute": "attribute",
        "attribute_value": "attribute_value",
        "from_range": "from_range",
        "to_range": "to_range",
        "increment": "increment",
        "numeric_values": "numeric_values"
    })

    # ------------------------------------------------------
    # FLAGS & SAVE
    # ------------------------------------------------------

    item.flags.ignore_permissions = True

    with active_item_during_opening_stock(item):
        item.save()

    src.db_set(
        "last_synced_on",
        sync_started_at,
        update_modified=False
    )

    return item.name
