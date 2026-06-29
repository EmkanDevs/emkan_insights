import frappe
import json
from frappe.utils import flt, cint

@frappe.whitelist()
def sync_payment_template_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        sync_external_payment_template(name)

    return "Success"

def sync_external_payment_template(external_name):
    # Load the custom external record
    external = frappe.get_doc("External Payment Terms Template", external_name)
    
    # 1. Define Target Name
    target_name = external.remote_id or external.name

    if frappe.db.exists("Payment Terms Template", target_name):
        return target_name

    # 2. Create Template Header
    ppt = frappe.new_doc("Payment Terms Template")
    ppt.name = target_name
    ppt.template_name = target_name
    
    # FIX: Use getattr to avoid AttributeError if field is missing
    ppt.template_description = getattr(external, "description", "") or getattr(external, "template_description", "")

    # 3. Map Terms Child Table with 100% Validation Logic
    external_terms = external.get("terms", [])
    total_terms = len(external_terms)
    running_total = 0

    for i, row in enumerate(external_terms):
        # Ensure we are pulling the correct field name for the portion
        raw_portion = getattr(row, "invoice_portion", 0)
        portion = flt(raw_portion, 2)
        
        # 🟢 THE MATH FIX: Force the last row to complete the 100%
        if i == total_terms - 1:
            portion = max(0, 100.0 - running_total)
        else:
            running_total += portion

        ppt.append("terms", {
            "payment_term": row.payment_term,
            "description": getattr(row, "description", ""),
            "invoice_portion": portion,
            "credit_days": cint(getattr(row, "credit_days", 0)),
            "due_date_based_on": getattr(row, "due_date_based_on", "Day(s) after invoice date")
        })

    # 4. Low-level Database Insertion (Bypasses standard sum validation)
    ppt.db_insert()

    # Insert Child Table Rows
    for term in ppt.terms:
        term.parent = ppt.name
        term.parenttype = "Payment Terms Template"
        term.parentfield = "terms"
        if not term.name: 
            term.set_new_name()
        term.db_insert()

    # 5. Finalize Sync
    external.db_set("remote_id", ppt.name)
    frappe.db.commit()

    return ppt.name