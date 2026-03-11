import frappe
import json

@frappe.whitelist()
def sync_project_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        external = frappe.get_doc("External Project", name)

        if not external.company:
            frappe.throw(f"Company missing in External Project: {name}")

        sync_external_project(name, external.company)

    return "Success"


def sync_external_project(external_name, company):
    external = frappe.get_doc("External Project", external_name)
    
    # 🟢 This is the ID we want to force
    target_name = external.remote_id or external.name

    # Check if it already exists to avoid SQL Duplicate errors
    if frappe.db.exists("Project", target_name):
        return target_name

    # ... [Keep your Parent and Cost Center resolution logic here] ...

    project = frappe.new_doc("Project")
    
    # 1. Set the name directly on the object
    project.name = target_name
    
    # 2. Map your fields
    project.project_name = external.project_name
    project.company = company
    project.status = external.status or "Open"
    project.expected_start_date = external.expected_start_date
    project.expected_end_date = external.expected_end_date

    # 3. 🟢 THE BYPASS: Write directly to the DB
    # This ignores autoname(), naming series, and all controller validations
    project.db_insert()

    # 4. Manually trigger standard post-insert logic if needed
    # (Optional) project.run_post_save_methods() 

    return project.name