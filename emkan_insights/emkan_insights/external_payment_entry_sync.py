import frappe
import json


# ==========================================================
# CONSTANTS
# ==========================================================

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_payment_entry_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            result = sync_external_payment_entry(source_doctype, name)
            results.append({"name": name, "status": "synced", "payment_entry": result})

        except Exception:
            error = frappe.get_traceback()

            frappe.log_error(
                title=f"Payment Entry Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed"
            })

    return results


# ==========================================================
# MAIN SYNC FUNCTION
# ==========================================================

def sync_external_payment_entry(source_doctype, external_name):

    src = frappe.get_doc(source_doctype, external_name)

    # ------------------------------------------------------
    # Prevent duplicate
    # ------------------------------------------------------

    existing = frappe.db.get_value(
        "Payment Entry",
        {"remote_id": src.name},
        "name"
    )

    if existing:
        return existing

    # ------------------------------------------------------
    # Dependency Checks
    # ------------------------------------------------------

    if src.party_type and src.party:
        if not frappe.db.exists(src.party_type, src.party):
            frappe.throw(f"{src.party_type} {src.party} not found. Sync master first.")

    if src.paid_from and not frappe.db.exists("Account", src.paid_from):
        frappe.throw(f"Account {src.paid_from} not found.")

    if src.paid_to and not frappe.db.exists("Account", src.paid_to):
        frappe.throw(f"Account {src.paid_to} not found.")

    # ------------------------------------------------------
    # Create Payment Entry
    # ------------------------------------------------------

    doc = frappe.new_doc("Payment Entry")

    doc.remote_id = src.name

    # ------------------------------------------------------
    # Copy Main Fields
    # ------------------------------------------------------

    for field, value in src.as_dict().items():

        if (
            field not in SYSTEM_FIELDS
            and field not in ["references", "remote_id"]
            and hasattr(doc, field)
        ):
            doc.set(field, value)

    # ------------------------------------------------------
    # References Table
    # ------------------------------------------------------

    doc.set("references", [])

    for row in src.get("references") or []:

        ref_row = {}

        for field, value in row.as_dict().items():

            if field not in SYSTEM_FIELDS:
                ref_row[field] = value

        doc.append("references", ref_row)

    # ------------------------------------------------------
    # Safe Flags
    # ------------------------------------------------------

    doc.flags.ignore_validate = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True

    # ------------------------------------------------------
    # Insert
    # ------------------------------------------------------

    doc.insert(
        ignore_permissions=True,
        ignore_mandatory=True
    )

    # ------------------------------------------------------
    # FORCE SAME NAME AS EXTERNAL (same logic as DN / PR / PI)
    # ------------------------------------------------------

    if doc.name != src.name:

        frappe.db.sql("""
            UPDATE `tabPayment Entry`
            SET name = %s
            WHERE name = %s
        """, (src.name, doc.name))

        frappe.db.sql("""
            UPDATE `tabPayment Entry Reference`
            SET parent = %s
            WHERE parent = %s
        """, (src.name, doc.name))

        frappe.db.commit()

        doc.name = src.name

    # ------------------------------------------------------
    # Sync Docstatus
    # ------------------------------------------------------

    if src.docstatus == 1:
        doc.submit()

    elif src.docstatus == 2:
        doc.submit()
        doc.cancel()

    return doc.name