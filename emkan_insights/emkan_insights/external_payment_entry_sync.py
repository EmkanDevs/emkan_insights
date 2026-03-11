import frappe
import json


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_payment_entry_docs(source_doctype: str, names):

    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        sync_external_payment_entry(name)

    return "Success"


# ==========================================================
# MAIN SYNC FUNCTION
# ==========================================================

def sync_external_payment_entry(external_name):

    src = frappe.get_doc("External Payment Entry", external_name)

    if not src.company:
        frappe.throw(f"Company missing in External Payment Entry: {external_name}")

    # ------------------------------------------------------
    # Prevent duplicate (ERPNext is master)
    # ------------------------------------------------------
    existing = None

    if src.remote_id:
        existing = frappe.db.exists("Payment Entry", src.remote_id)

    if existing:
        return existing

    # ------------------------------------------------------
    # Dependency Checks
    # ------------------------------------------------------

    if src.party_type and src.party:
        if not frappe.db.exists(src.party_type, src.party):
            frappe.throw(f"{src.party_type} {src.party} not found. Sync master first.")

    if src.paid_from and not frappe.db.exists("Account", src.paid_from):
        frappe.throw(f"Account {src.paid_from} not found. Sync Account first.")

    if src.paid_to and not frappe.db.exists("Account", src.paid_to):
        frappe.throw(f"Account {src.paid_to} not found. Sync Account first.")

    # ------------------------------------------------------
    # Create Payment Entry
    # ------------------------------------------------------

    doc = frappe.new_doc("Payment Entry")

    doc.company = src.company
    doc.posting_date = src.posting_date
    doc.payment_type = src.payment_type
    doc.mode_of_payment = src.mode_of_payment

    doc.party_type = src.party_type
    doc.party = src.party

    doc.paid_from = src.paid_from
    doc.paid_to = src.paid_to
    doc.paid_amount = src.paid_amount
    doc.received_amount = src.received_amount

    doc.reference_no = src.reference_no
    doc.reference_date = src.reference_date
    doc.remarks = src.remarks

    # Optional external tracking fields
    if hasattr(doc, "source_site") and src.get("source_site"):
        doc.source_site = src.source_site

    # ------------------------------------------------------
    # References Table (If Exists)
    # ------------------------------------------------------

    for row in src.get("references") or []:
        doc.append("references", {
            "reference_doctype": row.reference_doctype,
            "reference_name": row.reference_name,
            "allocated_amount": row.allocated_amount
        })

    # ------------------------------------------------------
    # Safe Flags
    # ------------------------------------------------------

    doc.flags.ignore_validate = True
    doc.flags.ignore_permissions = True

    doc.insert()
    doc.submit()

    # ------------------------------------------------------
    # Write Back ERPNext Name to External
    # ------------------------------------------------------

    if hasattr(src, "remote_id") and src.remote_id != doc.name:
        src.db_set("remote_id", doc.name)

    return doc.name