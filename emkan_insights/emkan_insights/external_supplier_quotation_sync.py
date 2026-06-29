import frappe
import json


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_supplier_quotation_docs(source_doctype: str, names=None):

    if names:
        if isinstance(names, str):
            names = json.loads(names)

        result = sync_bulk_supplier_quotations(names)
        return f"Success: {result['success']}, Failed: {result['failed']}"

    else:
        all_names = frappe.get_all(
            "External Supplier Quotation",
            pluck="name",
            limit_page_length=0
        )

        if not all_names:
            return "No External Supplier Quotations found to sync"

        chunk_size = 200
        chunks = [all_names[i:i + chunk_size] for i in range(0, len(all_names), chunk_size)]

        for chunk in chunks:
            frappe.enqueue(
                "emkan_insights.emkan_insights.external_supplier_quotation_sync.sync_bulk_supplier_quotations",
                queue="long",
                timeout=3000,
                names=chunk
            )

        return f"{len(all_names)} records queued across {len(chunks)} background jobs"


# ==========================================================
# BULK SYNC
# ==========================================================

def sync_bulk_supplier_quotations(names):

    sources = frappe.get_all(
        "External Supplier Quotation",
        filters={"name": ["in", names]},
        fields=["name", "remote_id"],
        limit_page_length=0
    )

    full_docs = {
        d.name: frappe.get_doc("External Supplier Quotation", d.name)
        for d in sources
    }

    suppliers  = set(frappe.get_all("Supplier",  pluck="name", limit_page_length=0))
    items      = set(frappe.get_all("Item",      pluck="name", limit_page_length=0))
    warehouses = set(frappe.get_all("Warehouse", pluck="name", limit_page_length=0))

    remote_ids = [d.remote_id for d in sources if d.remote_id]

    existing_sq = frappe.get_all(
        "Supplier Quotation",
        filters={"remote_id": ["in", remote_ids]},
        fields=["name", "remote_id", "docstatus"],
        limit_page_length=0
    ) if remote_ids else []

    existing_map = {d.remote_id: d for d in existing_sq}

    success      = 0
    failed       = 0
    failed_names = []

    for idx, name in enumerate(names, start=1):

        src = full_docs.get(name)

        try:
            if not src:
                raise Exception(f"Source doc not found: {name}")

            if not src.remote_id:
                raise Exception("remote_id is missing")

            if not src.company:
                raise Exception("Company is missing")

            if not src.supplier:
                raise Exception("Supplier is missing")

            existing = existing_map.get(src.remote_id)

            doc = upsert_supplier_quotation(
                src,
                existing_name=existing["name"] if existing else None,
                suppliers=suppliers,
                items=items,
                warehouses=warehouses
            )

            sync_docstatus(doc, src.docstatus)
            success += 1

        except Exception as e:
            failed += 1
            failed_names.append(name)

            frappe.log_error(
                title="Supplier Quotation Sync Failed",
                message=(
                    f"External SQTN : {name}\n"
                    f"Remote ID     : {getattr(src, 'remote_id', 'N/A') if src else 'N/A'}\n"
                    f"Error         : {str(e)}"
                )
            )

        if idx % 50 == 0:
            frappe.db.commit()

    frappe.db.commit()

    if failed_names:
        frappe.log_error(
            title="Supplier Quotation Sync — Batch Failed Names",
            message="\n".join(failed_names)
        )

    return {"success": success, "failed": failed}


# ==========================================================
# UPSERT
# ==========================================================

def upsert_supplier_quotation(src, existing_name=None, suppliers=None, items=None, warehouses=None):

    if not src.remote_id:
        raise Exception(f"External SQTN {src.name} has no remote_id")

    # ----------------------------------------------------------
    # LOAD OR CREATE
    # ----------------------------------------------------------
    if existing_name:
        doc = frappe.get_doc("Supplier Quotation", existing_name)
    else:
        if frappe.db.exists("Supplier Quotation", src.remote_id):
            return frappe.get_doc("Supplier Quotation", src.remote_id)

        doc = frappe.new_doc("Supplier Quotation")
        doc.name = src.remote_id
        doc.flags.name_set = True

    # ----------------------------------------------------------
    # VALIDATIONS
    # ----------------------------------------------------------
    if src.supplier not in suppliers:
        raise Exception(f"Supplier '{src.supplier}' not found in ERPNext")

    # ----------------------------------------------------------
    # HEADER FIELDS
    # ----------------------------------------------------------
    doc.company           = src.company
    doc.supplier          = src.supplier
    doc.transaction_date  = src.transaction_date
    doc.valid_till        = src.valid_till
    doc.currency          = src.currency
    doc.buying_price_list = src.buying_price_list
    doc.conversion_rate   = src.conversion_rate or 1

    if hasattr(doc, "remote_id"):
        doc.remote_id = src.remote_id

    if hasattr(doc, "source_site") and src.get("source_site"):
        doc.source_site = src.source_site

    # ----------------------------------------------------------
    # TAX FIELDS
    # ----------------------------------------------------------
    if src.get("tax_category"):
        doc.tax_category = src.tax_category

    if src.get("taxes_and_charges"):
        doc.taxes_and_charges = src.taxes_and_charges

    # ----------------------------------------------------------
    # ITEMS
    # ----------------------------------------------------------
    doc.set("items", [])

    if not src.items:
        raise Exception(f"No items found in External SQTN: {src.name}")

    for row in src.items:

        if row.item_code not in items:
            raise Exception(f"Item '{row.item_code}' not found in ERPNext")

        if row.warehouse and row.warehouse not in warehouses:
            raise Exception(f"Warehouse '{row.warehouse}' not found in ERPNext")

        qty  = row.qty  or 0
        rate = row.rate or 0

        doc.append("items", {
            "item_code"         : row.item_code,
            "item_name"         : row.item_name,
            "description"       : row.get("description") or row.item_name,
            "uom"               : row.uom,
            "qty"               : qty,
            "rate"              : rate,
            "amount"            : qty * rate,
            "conversion_factor" : getattr(row, "conversion_factor", 1) or 1,
            "base_rate"         : getattr(row, "base_rate",   rate)      or rate,
            "base_amount"       : getattr(row, "base_amount", qty * rate),
            "warehouse"         : row.warehouse,
        })

    # ----------------------------------------------------------
    # TAXES CHILD TABLE
    # ----------------------------------------------------------
    doc.set("taxes", [])

    for tax in (src.get("taxes") or []):
        doc.append("taxes", {
            "charge_type"     : tax.get("charge_type") or "On Net Total",
            "account_head"    : tax.get("account_head"),
            "description"     : tax.get("description") or tax.get("account_head"),
            "rate"            : tax.get("rate") or 0,
            "tax_amount"      : tax.get("tax_amount") or 0,
            "total"           : tax.get("total") or 0,
            "base_tax_amount" : tax.get("base_tax_amount") or tax.get("tax_amount") or 0,
            "base_total"      : tax.get("base_total") or tax.get("total") or 0,
        })

    # ----------------------------------------------------------
    # FLAGS
    # ----------------------------------------------------------
    doc.flags.ignore_permissions  = True
    doc.flags.ignore_links        = True
    doc.flags.ignore_mandatory    = True
    doc.flags.ignore_pricing_rule = True
    doc.flags.ignore_validate     = True

    if hasattr(doc, "status"):
        doc.set("status", None)

    # ----------------------------------------------------------
    # SAVE
    # ----------------------------------------------------------
    if existing_name:
        doc.save()
    else:
        doc.insert()

    return doc


# ==========================================================
# DOCSTATUS SYNC
# ==========================================================

def sync_docstatus(doc, target_status):

    current = doc.docstatus

    if target_status == 0:
        if current in (1, 2):
            frappe.throw(f"Cannot revert SQTN {doc.name} to Draft — already {current}")

    elif target_status == 1:
        if current == 0:
            doc.submit()
        elif current == 2:
            frappe.throw(f"Cannot resubmit cancelled SQTN {doc.name}")

    elif target_status == 2:
        if current == 0:
            doc.submit()
            doc.cancel()
        elif current == 1:
            doc.cancel()