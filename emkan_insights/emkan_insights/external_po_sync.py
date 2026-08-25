import frappe
import json
from frappe.utils import flt, nowdate
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.sync_guard import (
    set_source_site_fields,
    sync_record_guard,
    temporarily_enable_transaction_items,
)

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


# ==========================================================
# HELPERS
# ==========================================================
def sync_docstatus(doc, target_status):
    """
    Synchronise a Purchase Order's docstatus to match the source using the
    ORM submit()/cancel() path, which correctly runs all ERPNext lifecycle
    hooks (GL Entries, reserved quantities, linked document statuses, etc.).

    If ORM submit or cancel fails, the error is logged as CRITICAL and
    re-raised so the caller's savepoint rolls back only this record.
    Raw SQL docstatus flip is intentionally NOT used as a fallback.
    """
    target = int(target_status)
    current = int(frappe.db.get_value(doc.doctype, doc.name, "docstatus") or 0)

    if current == target:
        return

    try:
        doc.flags.ignore_permissions = True

        if target == 1:
            if current == 0:
                with temporarily_enable_transaction_items(doc):
                    doc.submit()  # ORM submit — runs all lifecycle hooks
            else:
                frappe.log_error(
                    title=f"PO {doc.name} unhandled docstatus transition: {current} → {target}",
                    message="Only Draft→Submitted and Draft/Submitted→Cancelled are handled."
                )

        elif target == 2:
            if current == 0:
                with temporarily_enable_transaction_items(doc):
                    doc.submit()  # Must submit first before cancel
                    doc.reload()
                    doc.cancel()  # ORM cancel — runs all lifecycle hooks
            else:
                with temporarily_enable_transaction_items(doc):
                    doc.cancel()  # ORM cancel — runs all lifecycle hooks

        elif target == 0:
            frappe.log_error(
                title=f"PO {doc.name} cannot revert to Draft",
                message=(
                    f"ERPNext does not support reverting a submitted/cancelled document "
                    f"to Draft via the ORM. Current docstatus: {current}. "
                    f"This is a data inconsistency on the source system."
                )
            )

    except Exception as err:
        frappe.log_error(
            title=f"CRITICAL: PO {doc.name} ORM lifecycle failed ({current} → {target}) — manual intervention required",
            message=(
                f"Error: {str(err)}\n\n{frappe.get_traceback()}\n\n"
                f"The Purchase Order is stuck at docstatus={current}. "
                f"Investigate why submit()/cancel() failed and correct the document "
                f"manually via ERPNext."
            )
        )
        raise


def _force_docstatus_db(name, doctype, target_docstatus):
    """
    INTENTIONALLY DISABLED: Force-setting docstatus via raw SQL bypasses all
    ERPNext submit/cancel hooks (GL Entries, reserved quantities, linked
    document statuses, etc.).

    This stub is kept so any remaining call sites surface a clear error instead
    of silently corrupting data. Use sync_docstatus() (ORM path) instead.
    """
    raise frappe.ValidationError(
        f"_force_docstatus_db('{name}', '{doctype}', {target_docstatus}) is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks. "
        f"Use the ORM submit()/cancel() path via sync_docstatus()."
    )


def resolve_account(account_head, company, company_abbr=None):
    """
    Resolve an account head to a valid account name in this company.
    Tries exact match first, then matches by the numeric code prefix
    (before the first space).
    """
    return resolve_company_account(account_head, company, company_abbr)


def _resolve_material_request(remote_mr_name, company_abbr=None):
    """
    Three-tier resolution, same pattern as RFQ sync:
    1. Prefixed local name: {company_abbr}-{remote_name}
    2. remote_id / custom_remote_id lookup
    3. Raw name existence check
    """
    if not remote_mr_name:
        return None

    if company_abbr and not remote_mr_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_mr_name}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed

    for field in ("remote_id", "custom_remote_id"):
        if frappe.get_meta("Material Request").has_field(field):
            found = frappe.db.get_value("Material Request", {field: remote_mr_name}, "name")
            if found:
                return found

    if frappe.db.exists("Material Request", remote_mr_name):
        return remote_mr_name

    return None


def _resolve_link_with_abbr(doctype, remote_name, company_abbr=None):
    if not remote_name:
        return None

    candidates = [remote_name]
    if company_abbr and not remote_name.startswith(f"{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_name}")
    elif company_abbr and remote_name.startswith(f"{company_abbr}-"):
        # Also try unprefixed remote_id (SQ stores custom_remote_id = PUR-SQTN-…)
        stripped = remote_name[len(f"{company_abbr}-"):]
        if stripped:
            candidates.append(stripped)

    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        if frappe.db.exists(doctype, name):
            return name

    meta = frappe.get_meta(doctype)
    for name in candidates:
        if not name:
            continue
        for field in ("remote_id", "custom_remote_id"):
            if meta.has_field(field):
                local_name = frappe.db.get_value(doctype, {field: name}, "name")
                if local_name:
                    return local_name

    # Bridge via External Supplier Quotation → synced SQ name
    if doctype == "Supplier Quotation":
        for name in candidates:
            if not name:
                continue
            if frappe.db.exists("External Supplier Quotation", name):
                linked = frappe.db.get_value(
                    "External Supplier Quotation", name, "remote_id"
                )
                if linked and frappe.db.exists("Supplier Quotation", linked):
                    return linked
            if meta.has_field("custom_remote_id"):
                local_name = frappe.db.get_value(
                    "Supplier Quotation", {"custom_remote_id": name}, "name"
                )
                if local_name:
                    return local_name
            if meta.has_field("remote_id"):
                local_name = frappe.db.get_value(
                    "Supplier Quotation", {"remote_id": name}, "name"
                )
                if local_name:
                    return local_name

    return None


def _resolve_supplier_quotation_item(sq_name, remote_item_id, item_code=None):
    """
    Map remote Supplier Quotation Item id → local child row name.
    External SQ sync stores the remote child id in custom_remote_id.
    """
    if not remote_item_id:
        return None

    # Already a local row name
    if sq_name and frappe.db.exists(
        "Supplier Quotation Item", {"name": remote_item_id, "parent": sq_name}
    ):
        return remote_item_id
    if frappe.db.exists("Supplier Quotation Item", remote_item_id):
        parent = frappe.db.get_value("Supplier Quotation Item", remote_item_id, "parent")
        if not sq_name or parent == sq_name:
            return remote_item_id

    filters = {"custom_remote_id": remote_item_id}
    if sq_name:
        filters["parent"] = sq_name

    if frappe.get_meta("Supplier Quotation Item").has_field("custom_remote_id"):
        found = frappe.db.get_value("Supplier Quotation Item", filters, "name")
        if found:
            return found

    # Fallback: match by item_code under the resolved SQ parent
    if sq_name and item_code:
        found = frappe.db.get_value(
            "Supplier Quotation Item",
            {"parent": sq_name, "item_code": item_code},
            "name",
        )
        if found:
            return found

    return None


def _build_target_name(company_abbr, remote_id):
    if company_abbr and remote_id.startswith(f"{company_abbr}-"):
        return remote_id
    return f"{company_abbr}-{remote_id}" if company_abbr else remote_id


def _find_existing_po(remote_id, target_name):
    """
    Check by remote_id first (authoritative link back to source),
    then fall back to the prefixed target name.
    """
    existing = frappe.db.get_value("Purchase Order", {"remote_id": remote_id}, "name")
    if existing:
        return existing
    if frappe.db.exists("Purchase Order", target_name):
        return target_name
    return None


def _get_buying_price_list(src):
    if src.buying_price_list and frappe.db.exists("Price List", src.buying_price_list):
        return src.buying_price_list
    return None


def _apply_po_header_fields(doc, src, company_abbr, conversion_rate):
    doc.update({
        "company": src.company,
        "supplier": src.supplier,
        "supplier_name": src.supplier_name,
        "transaction_date": src.transaction_date or nowdate(),
        "schedule_date": src.schedule_date or src.transaction_date or nowdate(),
        "currency": src.currency or "SAR",
        "conversion_rate": conversion_rate,
        "plc_conversion_rate": flt(src.plc_conversion_rate) or 1.0,
        "remote_id": src.remote_id,
        "buying_price_list": _get_buying_price_list(src),
        "apply_discount_on": src.apply_discount_on or "Net Total",
        "disable_rounded_total": src.disable_rounded_total or 0,
        "taxes_and_charges": None,
    })

    src_site_value = getattr(src, "site_url", None) or getattr(src, "source_site", None)
    if src_site_value:
        set_source_site_fields(doc, site_url=src_site_value)

    doc.total = flt(src.total)
    doc.net_total = flt(src.net_total) or flt(doc.total)
    doc.base_total = flt(src.base_total)
    doc.base_net_total = flt(src.base_net_total) or flt(doc.base_total)
    doc.total_qty = flt(src.total_qty)
    doc.grand_total = flt(src.grand_total)
    doc.base_grand_total = flt(src.base_grand_total)
    doc.base_taxes_and_charges_added = flt(src.base_taxes_and_charges_added)
    doc.taxes_and_charges_added = flt(src.taxes_and_charges_added)
    doc.base_taxes_and_charges_deducted = flt(src.base_taxes_and_charges_deducted)
    doc.taxes_and_charges_deducted = flt(src.taxes_and_charges_deducted)
    doc.total_taxes_and_charges = flt(src.total_taxes_and_charges)
    doc.base_total_taxes_and_charges = flt(src.base_total_taxes_and_charges)
    doc.tax_withholding_net_total = flt(getattr(src, "tax_withholding_net_total", 0))
    doc.base_tax_withholding_net_total = flt(getattr(src, "base_tax_withholding_net_total", 0))
    doc.rounding_adjustment = flt(src.rounding_adjustment)
    doc.base_rounding_adjustment = flt(src.base_rounding_adjustment)
    doc.rounded_total = flt(src.rounded_total)
    doc.base_rounded_total = flt(src.base_rounded_total)
    doc.in_words = getattr(src, "in_words", "")
    doc.base_in_words = getattr(src, "base_in_words", "")


def _prepare_existing_draft_po(src, existing_name, company_abbr, conversion_rate):
    doc = frappe.get_doc("Purchase Order", existing_name)
    if int(doc.docstatus or 0) != 0:
        frappe.throw(
            f"Purchase Order {existing_name} is submitted/cancelled and cannot be updated in place. "
            "Cancel and amend it through ERPNext lifecycle before syncing changed source data."
        )

    doc.flags.ignore_permissions = True
    _apply_po_header_fields(doc, src, company_abbr, conversion_rate)
    doc.set("items", [])
    doc.set("taxes", [])
    doc.set("payment_schedule", [])
    doc.save(ignore_permissions=True, ignore_mandatory=True)
    return doc.name


def _insert_new_po(src, target_name, company_abbr, conversion_rate):
    doc = frappe.new_doc("Purchase Order")
    doc.name = target_name
    doc.flags.name_set = True
    _apply_po_header_fields(doc, src, company_abbr, conversion_rate)
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    return doc.name


# ==========================================================
# SAFE CHILD INSERTION (dedup by source row identity, not content)
# ==========================================================

def _insert_po_items_safely(po_name, src, company_abbr=None, conversion_rate=1.0):
    """
    Insert Purchase Order Items one at a time.
    Dedup uses the SOURCE ROW'S identity (row.name / custom_remote_id),
    never (item_code, qty, rate) - that fingerprint incorrectly collapses
    legitimate repeated line items (e.g. same item ordered 3x separately).

    Also writes the matched internal item's name back onto the source
    row's custom_remote_id-tracking field so re-syncs stay idempotent -
    this write-back happens in upsert_purchase_order() after insertion,
    once we know the real child names.
    """
    if not src.items:
        return 0, [], []

    success_count = 0
    errors = []
    seen_source_rows = set()
    inserted_map = []  # (source_row_name, new_child_name) for write-back

    for idx, row in enumerate(src.items, start=1):
        try:
            row_identifier = getattr(row, "custom_remote_id", None) or row.name
            if row_identifier in seen_source_rows:
                frappe.log_error(
                    title=f"PO {po_name} duplicate item skipped",
                    message=f"Source row {row_identifier} (idx {idx}) already processed "
                            f"in this sync run - duplicate data in External PO source."
                )
                continue
            seen_source_rows.add(row_identifier)

            qty = flt(row.qty)
            rate = flt(row.rate)
            amount = flt(row.amount) if flt(row.amount) else qty * rate

            mr_name = _resolve_material_request(getattr(row, "material_request", None), company_abbr)
            mr_item_name = None
            if mr_name:
                mr_item_name = frappe.db.get_value(
                    "Material Request Item",
                    {"parent": mr_name, "item_code": row.item_code},
                    "name"
                )

            # Resolve Supplier Quotation parent + local child row name
            # (PO from remote carries the REMOTE SQ item id; local SQ items
            # store that id in custom_remote_id — must map to local name).
            sq_name = _resolve_link_with_abbr(
                "Supplier Quotation",
                getattr(row, "supplier_quotation", None),
                company_abbr,
            )
            sq_item_name = _resolve_supplier_quotation_item(
                sq_name,
                getattr(row, "supplier_quotation_item", None),
                item_code=row.item_code,
            )
            if sq_item_name:
                sq_rate = flt(frappe.db.get_value("Supplier Quotation Item", sq_item_name, "rate"))
                if sq_rate and rate and sq_rate != rate:
                    frappe.log_error(
                        title=f"PO {po_name} SQ link dropped",
                        message=(
                            f"External PO row {row.name} rate {rate} does not match "
                            f"Supplier Quotation Item {sq_item_name} rate {sq_rate}. "
                            f"Cleared supplier_quotation links so PO can preserve the "
                            f"source PO rate without failing submit validation."
                        ),
                    )
                    sq_name = None
                    sq_item_name = None

            new_child_name = frappe.generate_hash(length=10)

            item_row = {
                "doctype": "Purchase Order Item",
                "name": new_child_name,
                "parent": po_name,
                "parentfield": "items",
                "parenttype": "Purchase Order",
                "idx": idx,
                "item_code": row.item_code,
                "item_name": row.item_name or row.item_code,
                "description": getattr(row, "description", None) or row.item_name or row.item_code,
                "custom_remote_id": getattr(row, "custom_remote_id", None) or row.name,
                "qty": qty,
                "stock_qty": qty,
                "rate": rate,
                "amount": amount,
                "net_rate": flt(row.net_rate) if flt(getattr(row, "net_rate", 0)) else rate,
                "net_amount": flt(row.net_amount) if flt(getattr(row, "net_amount", 0)) else amount,
                "base_rate": rate * conversion_rate,
                "base_amount": amount * conversion_rate,
                "base_net_rate": (flt(row.net_rate) or rate) * conversion_rate,
                "base_net_amount": (flt(row.net_amount) or amount) * conversion_rate,
                "uom": row.uom or "Nos",
                "stock_uom": getattr(row, "stock_uom", None) or row.uom or "Nos",
                "conversion_factor": 1.0,
                "warehouse": getattr(row, "warehouse", None),
                "schedule_date": row.schedule_date or frappe.db.get_value("Purchase Order", po_name, "schedule_date"),
                "project": getattr(row, "project", None) or getattr(src, "project", None),
                "cost_center": getattr(row, "cost_center", None),
                "price_list_rate": flt(getattr(row, "price_list_rate", 0)),
                "base_price_list_rate": flt(getattr(row, "base_price_list_rate", 0)),
                "discount_percentage": flt(getattr(row, "discount_percentage", 0)),
                "discount_amount": flt(getattr(row, "discount_amount", 0)),
                "item_tax_rate": getattr(row, "item_tax_rate", "{}") or "{}",
                "against_blanket_order": 0,
                "blanket_order": None,
                "blanket_order_rate": 0,
                "apply_tds": getattr(row, "apply_tds", 0),
                "sales_order": _resolve_link_with_abbr("Sales Order", getattr(row, "sales_order", None), company_abbr),
                "sales_order_item": getattr(row, "sales_order_item", None),
                "supplier_quotation": sq_name,
                "supplier_quotation_item": sq_item_name,
                "material_request": mr_name,
                "material_request_item": mr_item_name,
                "expense_account": resolve_account(
                    getattr(row, "expense_account", None), src.company, company_abbr
                ),
            }

            child_doc = frappe.get_doc(item_row)
            child_doc.db_insert()
            success_count += 1
            inserted_map.append((row.name, new_child_name))

        except Exception as e:
            error_msg = f"Item {idx} (row: {row.name}, item: {row.item_code}): {str(e)}"
            errors.append(error_msg)
            frappe.log_error(title=f"PO item insert failed: {po_name}", message=error_msg)

    return success_count, errors, inserted_map


def _insert_po_taxes_safely(po_name, src, company_abbr=None, conversion_rate=1.0):
    if not src.taxes:
        return 0, []

    success_count = 0
    errors = []
    seen_tax_rows = set()

    for idx, tax in enumerate(src.taxes, start=1):
        try:
            tax_key = (
                getattr(tax, "charge_type", None) or "On Net Total",
                getattr(tax, "account_head", None),
                flt(getattr(tax, "rate", 0)),
                flt(getattr(tax, "tax_amount", 0)),
                getattr(tax, "add_deduct_tax", None) or "Add",
                getattr(tax, "category", None) or "Total",
            )
            if tax_key in seen_tax_rows:
                continue
            seen_tax_rows.add(tax_key)

            acc = resolve_account(tax.account_head, src.company, company_abbr)
            if not acc:
                errors.append(f"Tax {idx}: account not found: {tax.account_head}")
                continue

            tax_amount = flt(tax.tax_amount)
            base_tax_amount = tax_amount * conversion_rate

            tax_row = {
                "doctype": "Purchase Taxes and Charges",
                "name": frappe.generate_hash(length=10),
                "parent": po_name,
                "parentfield": "taxes",
                "parenttype": "Purchase Order",
                "idx": idx,
                "charge_type": tax.charge_type or "On Net Total",
                "account_head": acc,
                "description": tax.description or acc,
                "rate": flt(tax.rate),
                "tax_amount": tax_amount,
                "tax_amount_after_discount_amount": flt(getattr(tax, "tax_amount_after_discount_amount", 0)) or tax_amount,
                "base_tax_amount": base_tax_amount,
                "base_tax_amount_after_discount_amount": base_tax_amount,
                "total": flt(getattr(tax, "total", 0)),
                "base_total": flt(getattr(tax, "base_total", 0)),
                "add_deduct_tax": tax.add_deduct_tax or "Add",
                "category": tax.category or "Total",
                "included_in_print_rate": getattr(tax, "included_in_print_rate", 0),
                "included_in_paid_amount": getattr(tax, "included_in_paid_amount", 0),
                "item_wise_tax_detail": getattr(tax, "item_wise_tax_detail", "{}") or "{}",
                "cost_center": getattr(tax, "cost_center", None),
            }

            child_doc = frappe.get_doc(tax_row)
            child_doc.db_insert()
            success_count += 1

        except Exception as e:
            error_msg = f"Tax {idx}: {str(e)}"
            errors.append(error_msg)
            frappe.log_error(title=f"PO tax insert failed: {po_name}", message=error_msg)

    return success_count, errors


# ==========================================================
# MAIN UPSERT
# ==========================================================

def upsert_purchase_order(src, existing_name):
    """Every raw SQL statement and
    ORM call below runs inside the same open transaction; the caller
    (sync_bulk_purchase_orders) decides when this record's work becomes
    durable.
    """
    company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""
    conversion_rate = flt(src.conversion_rate) or 1.0
    target_name = _build_target_name(company_abbr, src.remote_id or src.name)

    if existing_name:
        po_name = _prepare_existing_draft_po(
            src,
            existing_name,
            company_abbr,
            conversion_rate,
        )
    else:
        po_name = _insert_new_po(src, target_name, company_abbr, conversion_rate)

    # ----------------------------------------------------------
    # Insert children
    # ----------------------------------------------------------
    item_success, item_errors, inserted_map = _insert_po_items_safely(po_name, src, company_abbr, conversion_rate)
    if item_errors:
        frappe.log_error(
            title=f"PO {po_name} - {len(item_errors)} item errors",
            message="\n".join(item_errors)
        )

    tax_success, tax_errors = _insert_po_taxes_safely(po_name, src, company_abbr, conversion_rate)
    if tax_errors:
        frappe.log_error(
            title=f"PO {po_name} - {len(tax_errors)} tax errors",
            message="\n".join(tax_errors)
        )

    # ----------------------------------------------------------
    # Verify counts
    # ----------------------------------------------------------
    actual_items = frappe.db.count("Purchase Order Item", {"parent": po_name})
    actual_taxes = frappe.db.count("Purchase Taxes and Charges", {"parent": po_name, "parenttype": "Purchase Order"})
    expected_items = len(src.items or [])
    expected_taxes = len(src.taxes or [])

    if actual_items != expected_items:
        frappe.log_error(
            title=f"PO Sync items mismatch: {po_name}",
            message=f"Expected {expected_items} items, found {actual_items}. "
                    f"Successfully inserted: {item_success}, Errors: {len(item_errors)}. "
                    f"This may indicate duplicate source rows in External PO - "
                    f"check the External PO's fetch/refetch logic, not this sync."
        )

    if actual_taxes != expected_taxes:
        frappe.log_error(
            title=f"PO Sync taxes mismatch: {po_name}",
            message=f"Expected {expected_taxes} taxes, found {actual_taxes}. Errors: {len(tax_errors)}"
        )

    # ----------------------------------------------------------
    # Write back custom_remote_id onto the SOURCE (External PO) items,
    # so future refetches/syncs of this same row are traceable both ways.
    # ----------------------------------------------------------
    if frappe.get_meta("Purchase Order Item").has_field("custom_remote_id"):
        has_source_child = frappe.db.exists("DocType", "External Purchase Order Item")
        source_item_meta = frappe.get_meta("External Purchase Order Item") if has_source_child else None
        if has_source_child and source_item_meta.has_field("custom_remote_id"):
            for source_row_name, new_child_name in inserted_map:
                # only write if the External PO item row doesn't already carry it
                current = frappe.db.get_value("External Purchase Order Item", source_row_name, "custom_remote_id")
                if not current:
                    frappe.db.set_value(
                        "External Purchase Order Item", source_row_name,
                        "custom_remote_id", new_child_name,
                        update_modified=False
                    )

    # ----------------------------------------------------------
    # Write back remote_id onto the External PO parent
    # ----------------------------------------------------------
    if frappe.get_meta("External Purchase Order").has_field("remote_id"):
        frappe.db.set_value("External Purchase Order", src.name, "remote_id", po_name, update_modified=False)

    # ----------------------------------------------------------
    # Reload and sync docstatus
    # ----------------------------------------------------------
    frappe.clear_document_cache("Purchase Order", po_name)
    doc = frappe.get_doc("Purchase Order", po_name)
    sync_docstatus(doc, src.docstatus)

    return doc


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_purchase_order_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    # If no names passed, fetch unsynced
    if not names:
        names = frappe.db.sql(f"""
            SELECT name
            FROM `tab{source_doctype}`
            WHERE
                last_synced_on IS NULL
                OR modified >= last_synced_on
        """, pluck=True)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"po_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_po_sync.sync_bulk_purchase_orders",
            queue="long",
            job_name=job_name,
            timeout=2000,
            names=chunk,
        )
        queued_chunks += 1

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def sync_bulk_purchase_orders(names):
    """
    Runs as a background job. No explicit commits anywhere in this loop -
    Frappe's job runner commits automatically once this function returns
    without an uncaught exception (and rolls back the whole job if one
    does). Each External PO gets its own savepoint so one bad record can
    be cleanly discarded without losing the records already processed
    earlier in the same batch.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            # Move savepoint INSIDE try: if a previous loop broke the transaction state,
            # creating a savepoint outside would crash the whole batch silently.
            savepoint = f"po_sync_{frappe.generate_hash(length=10)}"

            with sync_record_guard("External Purchase Order", name):
                frappe.db.savepoint(savepoint)

                src = frappe.get_doc("External Purchase Order", name)
                sync_started_at = src.modified

                company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""
                target_name = _build_target_name(company_abbr, src.remote_id or src.name)
                existing = _find_existing_po(src.remote_id or src.name, target_name)

                doc = upsert_purchase_order(src, existing)
                src.db_set(
                    "last_synced_on",
                    sync_started_at,
                    update_modified=False
                )

                results.append({
                    "name": name,
                    "status": "success",
                    "po_name": doc.name,
                    "items": len(doc.items),
                    "taxes": len(doc.taxes),
                    "docstatus": doc.docstatus
                })

        except Exception as e:
            # Safely rollback without crashing the loop if the transaction is broken
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
                
            import traceback
            error_trace = traceback.format_exc()
            
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e),
                "traceback": error_trace
            })
            
            # CRITICAL FIX: Log the error to Frappe's Error Log so it is visible!
            # Previously, errors were swallowed into the 'results' array which 
            # is discarded by the background worker, making it look like "no error".
            frappe.log_error(
                title=f"External PO Sync Failed: {name}",
                message=error_trace
            )
            break

    return results
