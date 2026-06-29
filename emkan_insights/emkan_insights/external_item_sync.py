import frappe
import json
from frappe.utils import cint, flt


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_items(names=None):

    """
    Dedicated bulk sync for External Item -> Item

    Handles:
    - batching
    - queueing
    - timeout prevention
    - large datasets
    """

    if isinstance(names, str):
        names = json.loads(names)

    # If no names passed, sync everything
    if not names:
        names = frappe.get_all(
            "External Item",
            pluck="name"
        )

    batch_size = 200

    for i in range(0, len(names), batch_size):

        batch = names[i:i + batch_size]

        frappe.enqueue(
            "emkan_insights.emkan_insights.external_item_sync.sync_item_batch",
            queue="long",
            timeout=5000,
            names=batch
        )

    return f"{len(names)} Items queued for sync"


# ==========================================================
# BATCH SYNC
# ==========================================================

def sync_item_batch(names):

    success = 0
    failed = 0

    for idx, source_name in enumerate(names, start=1):

        try:
            sync_single_item(source_name)
            success += 1

            # periodic commit
            if idx % 20 == 0:
                frappe.db.commit()

        except Exception:

            failed += 1

            frappe.log_error(
                title=f"External Item Sync Failed: {source_name}",
                message=frappe.get_traceback()
            )

    frappe.db.commit()

    return {
        "success": success,
        "failed": failed
    }


# ==========================================================
# SINGLE ITEM SYNC
# ==========================================================

def sync_single_item(source_name):

    src = frappe.get_doc("External Item", source_name)

    target_name = src.remote_id

    existing = None

    if target_name and frappe.db.exists("Item", target_name):
        existing = target_name

    # ------------------------------------------------------
    # LOAD / CREATE
    # ------------------------------------------------------

    if existing:
        item = frappe.get_doc("Item", existing)
    else:
        item = frappe.new_doc("Item")

        if target_name:
            item.name = target_name
            item.flags.name_set = True

    # ------------------------------------------------------
    # BASIC FIELDS
    # ------------------------------------------------------

    item.update({

        "custom_remote_id": src.name,
        "custom_source_site": src.source_site,

        "item_code": getattr(src, "item_code", None) or src.name,
        "item_name": getattr(src, "item_name", None) or getattr(src, "item_code", None) or src.name,
        "item_group": getattr(src, "item_group", None) or "All Item Groups",
        "stock_uom": getattr(src, "stock_uom", None) or "Nos",
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
        "weight_uom": getattr(src, "weight_uom", None),
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

        "default_bom": getattr(src, "default_bom", None),
        "grant_commission": cint(getattr(src, "grant_commission", 1)),

        "variant_of": getattr(src, "variant_of", None),
        "variant_based_on": getattr(src, "variant_based_on", None),

    })

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
            for target_key, source_key in field_map.items():
                val = getattr(row, source_key, None)
                if val is not None:
                    new_row[target_key] = val
            if new_row:
                item.append(target_field, new_row)

    # ------------------------------------------------------
    # 1. UOMs
    # ------------------------------------------------------

    sync_child_table("uoms", "uoms", {
        "uom": "uom",
        "conversion_factor": "conversion_factor"
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
        "warehouse": "warehouse",
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
        "customer_group": "customer_group",
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
    item.flags.ignore_mandatory = True
    item.flags.ignore_validate = True
    item.flags.ignore_links = True

    item.save()

    return item.name