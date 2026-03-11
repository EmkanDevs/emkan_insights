import frappe
import json

@frappe.whitelist()
def sync_stock_entry_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    last_synced = None

    for name in names:
        external = frappe.get_doc("External Stock Entry", name)

        if not external.company:
            frappe.throw(f"Company missing in External Stock Entry: {name}")

        last_synced = sync_external_stock_entry(external.name, external.company)

    return last_synced


def sync_external_stock_entry(external_name, company):
    external = frappe.get_doc("External Stock Entry", external_name)

    # 🟢 1. Define Target Name (Force Remote ID)
    target_name = external.remote_id or external.name

    if frappe.db.exists("Stock Entry", target_name):
        return target_name

    # 🟢 2. Create Stock Entry Header
    se = frappe.new_doc("Stock Entry")
    se.company = company
    se.stock_entry_type = external.stock_entry_type
    se.purpose = external.purpose
    se.posting_date = external.posting_date
    se.posting_time = external.posting_time
    se.inspection_required = external.inspection_required

    # Optional: preserve original name if allowed
    se.flags.ignore_naming_series = True
    se.name = target_name

    # 🟢 Optional Project Mapping
    if external.get("project"):
        se.project = frappe.db.get_value(
            "External Project", external.project, "remote_id"
        )

    # 🟢 3. Map Items Child Table
    for row in external.get("items", []):
        item_code = frappe.db.get_value(
            "External Item", row.item_code, "remote_id"
        )

        if not item_code:
            frappe.throw(f"Item mapping missing for External Item: {row.item_code}")

        # Resolve Warehouses
        s_warehouse = None
        if row.get("s_warehouse"):
            if not frappe.db.exists("Warehouse", row.s_warehouse):
                frappe.throw(f"Warehouse not found: {row.s_warehouse}")
            s_warehouse = row.s_warehouse

        t_warehouse = None
        if row.get("t_warehouse"):
            if not frappe.db.exists("Warehouse", row.t_warehouse):
                frappe.throw(f"Warehouse not found: {row.t_warehouse}")
            t_warehouse = row.t_warehouse

        se.append("items", {
            "item_code": item_code,
            "item_name": row.item_name,
            "description": row.description,
            "qty": row.qty,
            "uom": row.uom,
            "stock_uom": row.stock_uom,
            "conversion_factor": row.conversion_factor or 1,
            "s_warehouse": s_warehouse,
            "cost_center": row.cost_center,
            "t_warehouse": t_warehouse,
            "basic_rate": row.basic_rate,
            "amount": row.amount
        })

    # 🟢 4. Insert Using Standard ERPNext Flow
    se.insert(ignore_permissions=True)

    # 🟢 5. Proper Submission (Creates Ledger Entries)
    remote_status = frappe.utils.cint(external.docstatus)

    if remote_status == 1:
        se.submit()

    elif remote_status == 2:
        se.submit()
        se.cancel()

    # 🟢 6. Save Mapping Back
    external.db_set("remote_id", se.name)

    frappe.db.commit()

    return se.name