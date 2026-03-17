import frappe
import json

# Fields we should never copy
SYSTEM_FIELDS = {
    "name",
    "owner",
    "creation",
    "modified",
    "modified_by",
    "docstatus",
    "idx",
    "__last_sync_on",
    "amended_from",
    "amendment_date",
    "_comments",
    "_liked_by",
    "_assign",
    "_seen"
}

# Child tables we will copy manually
CHILD_TABLES = ["items", "taxes", "payment_schedule"]


@frappe.whitelist()
def sync_external_quotation_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    created = []

    for name in names:

        ext_doc = frappe.get_doc(source_doctype, name)

        quotation = frappe.new_doc("Quotation")

        # --------------------------
        # Copy main fields
        # --------------------------
        for field, value in ext_doc.as_dict().items():

            if field in SYSTEM_FIELDS:
                continue

            if field in CHILD_TABLES:
                continue

            if field in quotation.meta.get_valid_columns():
                quotation.set(field, value)

        # --------------------------
        # Copy Items
        # --------------------------
        for row in ext_doc.items:

            quotation.append("items", {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "description": row.description,
                "qty": row.qty,
                "uom": row.uom,
                "rate": row.rate,
                "discount_percentage": row.discount_percentage
            })

        # --------------------------
        # Copy Taxes
        # --------------------------
        for tax in ext_doc.taxes:

            quotation.append("taxes", {
                "charge_type": tax.charge_type,
                "account_head": tax.account_head,
                "description": tax.description,
                "rate": tax.rate
            })

        # --------------------------
        # Copy Payment Schedule
        # --------------------------
        for ps in ext_doc.payment_schedule:

            quotation.append("payment_schedule", {
                "payment_term": ps.payment_term,
                "due_date": ps.due_date,
                "invoice_portion": ps.invoice_portion
            })

        # --------------------------
        # Insert document
        # --------------------------
        quotation.insert(ignore_permissions=True)

        # --------------------------
        # Match docstatus
        # --------------------------
        if ext_doc.docstatus == 1:
            quotation.submit()

        elif ext_doc.docstatus == 2:
            quotation.submit()
            quotation.cancel()

        created.append(quotation.name)

    return {
        "created": created
    }