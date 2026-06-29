import frappe
import json


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "__last_sync_on", "amended_from",
    "amendment_date", "_comments", "_liked_by", "_assign", "_seen"
}

CHILD_TABLES = ["items", "taxes", "payment_schedule"]


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_quotation_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    created = []
    updated = []

    for name in names:

        src = frappe.get_doc(source_doctype, name)

        # --------------------------------------------------
        # DUPLICATE CHECK (remote_id)
        # --------------------------------------------------
        existing = frappe.get_value(
            "Quotation",
            {"remote_id": src.name},
            "name"
        )

        doc = upsert_quotation(src, existing)

        sync_docstatus(doc, src.docstatus)

        if existing:
            updated.append(doc.name)
        else:
            created.append(doc.name)

    frappe.db.commit()

    return {
        "created": created,
        "updated": updated
    }


# ==========================================================
# UPSERT (SAFE)
# ==========================================================

def upsert_quotation(src, existing_name=None):
    if existing_name:
        quotation = frappe.get_doc("Quotation", existing_name)
        
        # 🟢 FIX 1: If already submitted/cancelled, don't try to save/update 
        # unless you intend to cancel and re-sync.
        if quotation.docstatus > 0:
            # Option A: Skip update (Safest for synced records)
            return quotation
            
            # Option B: If you MUST update, you'd need to cancel/amend here.
            # quotation.cancel() 
    else:
        quotation = frappe.new_doc("Quotation")
        quotation.name = src.name
        quotation.flags.name_set = True

    # --------------------------------------------------
    # REQUIRED FIELDS (CRITICAL)
    # --------------------------------------------------
    quotation.remote_id = src.name
    quotation.company = src.company
    quotation.party_name = src.party_name
    quotation.quotation_to = src.quotation_to or "Customer"

    # --------------------------------------------------
    # MAP MAIN FIELDS
    # --------------------------------------------------
    for field, value in src.as_dict().items():

        if field in SYSTEM_FIELDS or field in CHILD_TABLES:
            continue

        if field in quotation.meta.get_valid_columns():
            quotation.set(field, value)

    # --------------------------------------------------
    # ITEMS (FULL SAFE)
    # --------------------------------------------------
    quotation.set("items", [])

    for row in src.items:

        qty = row.qty or 0
        rate = row.rate or 0

        quotation.append("items", {
            "item_code": row.item_code,
            "item_name": row.item_name,
            "description": row.description,
            "qty": qty,
            "uom": row.uom,

            # 🔥 REQUIRED FOR PRICING ENGINE
            "rate": rate,
            "amount": qty * rate,
            "base_rate": rate,
            "base_amount": qty * rate,
            "conversion_factor": getattr(row, "conversion_factor", 1) or 1,
            "discount_percentage": row.discount_percentage or 0
        })

    # --------------------------------------------------
    # TAXES (SAFE)
    # --------------------------------------------------
    quotation.set("taxes", [])

    for tax in src.taxes:
        quotation.append("taxes", {
            "charge_type": tax.charge_type,
            "account_head": tax.account_head,
            "description": tax.description,
            "rate": tax.rate or 0
        })

    # --------------------------------------------------
    # 🚫 REMOVE PAYMENT SCHEDULE (CRITICAL FIX)
    # --------------------------------------------------
    quotation.set("payment_schedule", [])

    # --------------------------------------------------
    # FLAGS (SAFE COMBINATION)
    # --------------------------------------------------
    quotation.flags.ignore_permissions = True
    quotation.flags.ignore_links = True
    quotation.flags.ignore_mandatory = True
    quotation.flags.ignore_pricing_rule = True

    # --------------------------------------------------
    # 🔥 FORCE ERPNext CALCULATION
    # --------------------------------------------------
    quotation.run_method("set_missing_values")
    quotation.run_method("calculate_taxes_and_totals")

    # --------------------------------------------------
    # SAVE
    # --------------------------------------------------
    if existing_name:
        quotation.save()
    else:
        quotation.insert()

    return quotation


# ==========================================================
# DOCSTATUS SYNC (SAFE)
# ==========================================================

def sync_docstatus(doc, target_status):

    current = doc.docstatus

    if target_status == 0:
        return

    elif target_status == 1:
        if current == 0:
            doc.submit()

    elif target_status == 2:
        if current == 0:
            doc.submit()
            doc.cancel()
        elif current == 1:
            doc.cancel()