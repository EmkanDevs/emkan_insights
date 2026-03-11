import frappe
import json

@frappe.whitelist()
def sync_purchase_receipt_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        external = frappe.get_doc("External Purchase Receipt", name)

        if not external.company:
            frappe.throw(f"Company missing in External Purchase Receipt: {name}")

        sync_external_pr(name, external.company)

    return "Success"


def sync_external_pr(external_name, company):
    external = frappe.get_doc("External Purchase Receipt", external_name)
    
    # 🟢 This is the ID we want to force
    target_name = external.remote_id or external.name

    # Check if it already exists to avoid SQL Duplicate errors
    if frappe.db.exists("Purchase Receipt", target_name):
        return target_name

    # ... [Keep your Parent and Cost Center resolution logic here] ...

    pr = frappe.new_doc("Purchase Receipt")
    
    # 1. Set the name directly on the object
    pr.name = target_name
    
    # 2. Map your fields
    pr.company = company
    pr.supplier = external.supplier
    pr.posting_date = external.posting_date
    pr.supplier_delivery_note = external.supplier_delivery_note
    pr.status = external.status or "Open"
    pr.conversion_rate = external.conversion_rate or 1
    pr.base_net_total = external.base_net_total

    for row in external.items:
        pr.append("items", {
            "item_code": row.item_code,
            "item_name": row.item_name,

            "qty": row.qty or 0,
            "received_qty": row.received_qty or row.qty or 0,
            "rejected_qty": row.rejected_qty or 0,

            "uom": row.uom,
            "stock_uom": row.stock_uom,

            "conversion_factor": row.conversion_factor or 1,

            "rate": row.rate or 0,
            "amount": row.amount or 0,
            "base_rate": row.rate or 0,
            "base_amount": row.amount or 0,

            "billed_amt": row.billed_amt or 0,

            "warehouse": row.warehouse,
            "project": row.project,
            "purchase_order": row.purchase_order,
            "purchase_order_item": row.purchase_order_item,
            "cost_center": row.cost_center,
            "schedule_date": row.schedule_date,
            "billed_amt": 0
        })

    pr.flags.ignore_permissions = True
    pr.flags.ignore_validate = True

    pr.billing_address = None
    pr.shipping_address = None

    pr.insert(ignore_permissions=True)

    if external.docstatus == 1:
        pr.flags.ignore_permissions = True
        pr.submit()

    return pr.name