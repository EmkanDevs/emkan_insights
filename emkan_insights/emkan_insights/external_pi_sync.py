import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "purchase_order_item",
    "purchase_receipt_item",
    "prevdoc_doctype",
    "prevdoc_docname"
}


# -----------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------

def get_po_detail(purchase_order, item_code):

    if not purchase_order:
        return None

    return frappe.db.get_value(
        "Purchase Order Item",
        {
            "parent": purchase_order,
            "item_code": item_code
        },
        "name"
    )


def get_pr_detail(purchase_receipt, item_code):

    if not purchase_receipt:
        return None

    return frappe.db.get_value(
        "Purchase Receipt Item",
        {
            "parent": purchase_receipt,
            "item_code": item_code
        },
        "name"
    )


# -----------------------------------------------------
# MAIN SYNC
# -----------------------------------------------------

@frappe.whitelist()
def sync_external_purchase_invoice_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:

        try:

            ext_pi = frappe.get_doc(source_doctype, name)

            # -------------------------------------------------
            # ALREADY SYNCED
            # -------------------------------------------------

            existing = frappe.db.get_value(
                "Purchase Invoice",
                {"remote_id": ext_pi.name},
                "name"
            )

            if not existing:
                existing = frappe.db.exists("Purchase Invoice", ext_pi.name)

            if existing:
                results.append({
                    "name": existing,
                    "status": "exists"
                })
                continue

            pi = frappe.new_doc("Purchase Invoice")

            # -------------------------------------------------
            # REMOTE TRACKING
            # -------------------------------------------------

            pi.remote_id = ext_pi.name

            if hasattr(pi, "source_site"):
                pi.source_site = ext_pi.source_site

            # -------------------------------------------------
            # COPY HEADER FIELDS
            # -------------------------------------------------

            for field, value in ext_pi.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "payment_schedule"]
                    and hasattr(pi, field)
                ):
                    pi.set(field, value)

            # Fix currency validation
            if not pi.currency:
                pi.currency = frappe.get_cached_value("Company", pi.company, "default_currency")

            # -------------------------------------------------
            # HANDLE RETURN
            # -------------------------------------------------

            if ext_pi.get("is_return"):

                if frappe.db.exists("Purchase Invoice", ext_pi.return_against):
                    pi.is_return = 1
                    pi.return_against = ext_pi.return_against
                else:
                    pi.is_return = 0
                    pi.return_against = None

            # -------------------------------------------------
            # ITEMS
            # -------------------------------------------------

            pi.set("items", [])

            for row in ext_pi.items:

                item_row = {}

                for field, value in row.as_dict().items():

                    if (
                        field not in SYSTEM_FIELDS
                        and field not in IGNORE_ITEM_FIELDS
                    ):
                        item_row[field] = value

                # PO mapping
                if row.purchase_order:
                    item_row["purchase_order"] = row.purchase_order
                    item_row["po_detail"] = get_po_detail(
                        row.purchase_order,
                        row.item_code
                    )

                # PR mapping
                if row.purchase_receipt:
                    item_row["purchase_receipt"] = row.purchase_receipt
                    item_row["pr_detail"] = get_pr_detail(
                        row.purchase_receipt,
                        row.item_code
                    )

                pi.append("items", item_row)

            # -------------------------------------------------
            # TAXES
            # -------------------------------------------------

            if ext_pi.get("taxes"):

                pi.set("taxes", [])

                for row in ext_pi.taxes:

                    tax_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value

                    pi.append("taxes", tax_row)

            # -------------------------------------------------
            # PAYMENT SCHEDULE
            # -------------------------------------------------

            if ext_pi.get("payment_schedule"):

                pi.set("payment_schedule", [])

                for row in ext_pi.payment_schedule:

                    pay_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            pay_row[field] = value

                    pi.append("payment_schedule", pay_row)

            # -------------------------------------------------
            # FLAGS
            # -------------------------------------------------

            pi.flags.ignore_permissions = True
            pi.flags.ignore_validate = True
            pi.flags.ignore_mandatory = True

            # -------------------------------------------------
            # INSERT
            # -------------------------------------------------

            pi.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # -------------------------------------------------
            # FORCE SAME NAME
            # -------------------------------------------------

            if pi.name != ext_pi.name:

                frappe.db.sql("""
                    UPDATE `tabPurchase Invoice`
                    SET name=%s
                    WHERE name=%s
                """, (ext_pi.name, pi.name))

                for child in [
                    "Purchase Invoice Item",
                    "Purchase Taxes and Charges",
                    "Payment Schedule"
                ]:

                    frappe.db.sql(f"""
                        UPDATE `tab{child}`
                        SET parent=%s
                        WHERE parent=%s
                    """, (ext_pi.name, pi.name))

                frappe.db.commit()

                pi.name = ext_pi.name

            # -------------------------------------------------
            # DOCSTATUS
            # -------------------------------------------------

            if ext_pi.docstatus == 1:
                pi.submit()

            elif ext_pi.docstatus == 2:
                pi.submit()
                pi.cancel()

            results.append({
                "name": pi.name,
                "status": "synced"
            })

        except Exception as e:

            error = frappe.get_traceback()

            frappe.log_error(
                title=f"Purchase Invoice Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results