import frappe
import json


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_purchase_order_docs(source_doctype: str, names):

    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_po_sync.sync_external_purchase_order",
            queue="long",
            external_name=name,
            timeout=600
        )

    return f"{len(names)} Purchase Orders queued for sync"


# ==========================================================
# MAIN SYNC FUNCTION
# ==========================================================

def sync_external_purchase_order(external_name):

    src = frappe.get_doc("External Purchase Order", external_name)

    if not src.company:
        frappe.throw(f"Company missing in External Purchase Order: {external_name}")

    # ------------------------------------------------------
    # Prevent duplicate using remote_id
    # ------------------------------------------------------
    existing = frappe.get_value(
        "Purchase Order",
        {"remote_id": src.remote_id},
        "name"
    )

    if existing:
        return existing

    # ------------------------------------------------------
    # Ensure dependencies
    # ------------------------------------------------------

    # Supplier
    if src.supplier and not frappe.db.exists("Supplier", src.supplier):
        frappe.throw(f"Supplier {src.supplier} not found. Sync Supplier first.")

    # Project
    if src.project and not frappe.db.exists("Project", src.project):
        frappe.throw(f"Project {src.project} not found. Sync Project first.")

    # ------------------------------------------------------
    # Create Purchase Order
    # ------------------------------------------------------

    doc = frappe.new_doc("Purchase Order")

    doc.company = src.company
    doc.supplier = src.supplier
    doc.transaction_date = src.transaction_date
    doc.schedule_date = src.schedule_date
    doc.project = src.project
    doc.currency = src.currency
    doc.buying_price_list = src.buying_price_list
    doc.conversion_rate = src.conversion_rate

    # External tracking fields
    if hasattr(doc, "remote_id"):
        doc.remote_id = src.remote_id

    if hasattr(doc, "source_site") and src.get("source_site"):
        doc.source_site = src.source_site

    # ------------------------------------------------------
    # Items
    # ------------------------------------------------------

    for row in src.items:

        # Ensure Cost Center exists
        if row.cost_center and not frappe.db.exists("Cost Center", row.cost_center):
            frappe.throw(f"Cost Center {row.cost_center} not found. Sync Cost Center first.")

        doc.append("items", {
            "item_code": row.item_code,
            "item_name": row.item_name,
            "uom": row.uom,
            "base_rate": row.base_rate,
            "base_amount": row.base_amount,
            "conversion_factor": row.conversion_factor,
            "qty": row.qty,
            "rate": row.rate,
            "warehouse": row.warehouse,
            "project": row.project,
            "cost_center": row.cost_center,
            "schedule_date": row.schedule_date
        })

    # ------------------------------------------------------
    # Safe flags
    # ------------------------------------------------------

    doc.flags.ignore_pricing_rule = True
    doc.flags.ignore_validate = True
    
    try:
        doc.insert(ignore_permissions=True)

        # ------------------------------------------------------
        # Force document name = remote_id
        # ------------------------------------------------------
        if src.remote_id and doc.name != src.remote_id:
            frappe.rename_doc(
                "Purchase Order",
                doc.name,
                src.remote_id,
                force=True,
            )
            doc.name = src.remote_id

        doc.submit()

    except Exception as e:
        frappe.log_error(
            title="Purchase Order Sync Failed",
            message=f"""
                External PO: {external_name}
                Remote ID: {src.remote_id}
                Error: {str(e)}
                """
        )
        frappe.throw(f"Purchase Order Sync Failed for External PO: {str(e)}")

    return doc.name