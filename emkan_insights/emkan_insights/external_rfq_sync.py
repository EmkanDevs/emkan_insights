import frappe
import json

@frappe.whitelist()
def sync_rfq_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        external = frappe.get_doc("External Request for Quotation", name)

        if not external.company:
            frappe.throw(f"Company missing in External RFQ: {name}")

        sync_external_rfq(name, external.company)

    return "Success"


def sync_external_rfq(external_name, company):
    external = frappe.get_doc("External Request for Quotation", external_name)

    # 🟢 1. Define Target Name (Force Remote ID)
    target_name = external.remote_id or external.name

    if frappe.db.exists("Request for Quotation", target_name):
        return target_name

    # 🟢 2. Create RFQ Header Object
    rfq = frappe.new_doc("Request for Quotation")
    rfq.name = target_name
    rfq.company = company
    rfq.transaction_date = external.transaction_date
    rfq.message_for_supplier = external.message_for_supplier
    rfq.status = external.status or "Draft"
    
    # Optional field mapping (Project)
    if external.get("project"):
        rfq.project = frappe.db.get_value("External Project", external.project, "remote_id")

    # 🟢 3. Map Suppliers Child Table
    for s in external.get("suppliers", []):
        supplier_code = frappe.db.get_value("External Supplier", s.supplier, "remote_id")
        if supplier_code:
            rfq.append("suppliers", {
                "supplier": supplier_code,
                "contact": s.contact
            })

    # 🟢 4. Map Items Child Table (Handling Warehouse)
    default_wh = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")

    for row in external.get("items", []):
        item_code = frappe.db.get_value("External Item", row.item_code, "remote_id")
        warehouse = frappe.db.get_value("External Warehouse", row.warehouse, "remote_id")
        
        # Bypass Warehouse mandatory validation for Stock Items
        # if not warehouse:
        #     warehouse = default_wh

        # rfq.append("items", {
        #     "item_code": item_code,
        #     "item_name": row.item_name,
        #     "description": row.description,
        #     "qty": row.qty,
        #     "uom": row.uom,
        #     "warehouse": warehouse,
        #     "schedule_date": row.schedule_date
        # })

    # 🟢 5. THE BYPASS: Low-level Database Insertion
    # This skips the RFQ controller's naming series and validation logic
    rfq.db_insert()

    # Insert Items (Child Table 1)
    for item in rfq.items:
        item.parent = rfq.name
        item.parenttype = "Request for Quotation"
        item.parentfield = "items"
        if not item.name: item.set_new_name()
        item.db_insert()

    # Insert Suppliers (Child Table 2)
    for supp in rfq.suppliers:
        supp.parent = rfq.name
        supp.parenttype = "Request for Quotation"
        supp.parentfield = "suppliers"
        if not supp.name: supp.set_new_name()
        supp.db_insert()

    # 🟢 6. Sync Docstatus (Draft=0, Submitted=1, Cancelled=2)
    remote_status = frappe.utils.cint(external.docstatus)
    if remote_status > 0:
        frappe.db.set_value("Request for Quotation", rfq.name, "docstatus", remote_status, update_modified=False)

    # 🟢 7. Finalize
    external.db_set("remote_id", rfq.name)
    frappe.db.commit()

    return rfq.name