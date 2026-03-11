import frappe
import json


@frappe.whitelist()
def sync_material_request_docs(source_doctype: str, names):

    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        external = frappe.get_doc("External Material Request", name)

        if not external.company:
            frappe.throw(f"Company missing in External Material Request: {name}")

        sync_external_material_request(name, external.company)

    return "Success"


def sync_external_material_request(external_name, company):
    external = frappe.get_doc("External Material Request", external_name)

    # 🟢 1. Define the target name (Force remote_id as name)
    target_name = external.remote_id or external.name

    # Prevent duplicate
    if frappe.db.exists("Material Request", target_name):
        return target_name

    # Resolve Project
    project = None
    if external.get("project"):
        project = frappe.db.get_value("External Project", external.project, "remote_id")

    # 🟢 2. Create the Document Object
    mr = frappe.new_doc("Material Request")
    mr.name = target_name
    mr.material_request_type = external.material_request_type
    mr.company = company
    mr.transaction_date = external.transaction_date
    mr.schedule_date = external.schedule_date
    mr.project = project
    mr.status = external.status or "Draft"

    # 🟢 3. Add Items with a fallback Warehouse check
    # We fetch a default warehouse just in case the mapping fails
    default_wh = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")

    for row in external.items:
        item_code = frappe.db.get_value("External Item", row.item_code, "remote_id")
        warehouse = frappe.db.get_value("External Warehouse", row.warehouse, "remote_id")
        
        # If mapping didn't find a warehouse, use the default to satisfy DB constraints
        if not warehouse:
            warehouse = default_wh

        mr.append("items", {
            "item_code": item_code,
            "item_name": row.item_name,
            "description": row.description,
            "qty": row.qty,
            "uom": row.uom,
            "schedule_date": row.schedule_date,
            "warehouse": warehouse,
            "project": project
        })

    # 🟢 4. THE BYPASS: db_insert
    # This skips the 'Warehouse is mandatory' check in the Material Request controller
    mr.db_insert()

    # Manually insert child table rows (db_insert doesn't handle children)
    for item in mr.items:
        item.parent = mr.name
        item.parenttype = "Material Request"
        item.parentfield = "items"
        if not item.name:
            item.set_new_name()
        item.db_insert()

    # 🟢 5. Force Docstatus (0=Draft, 1=Submit, 2=Cancel)
    remote_status = frappe.utils.cint(external.docstatus)
    if remote_status > 0:
        frappe.db.set_value("Material Request", mr.name, "docstatus", remote_status, update_modified=False)

    # Write Back ERPNext name to external doc
    external.db_set("remote_id", mr.name)
    frappe.db.commit()

    return mr.name
    