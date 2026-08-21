import frappe
import json
from frappe.model.document import Document
from frappe.utils import cint, flt
from contextlib import contextmanager
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.sync_guard import sync_record_guard
from frappe import _
class ExternalStockEntry(Document):
    pass


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation", "quotation_item", "prevdoc_doctype",
    "prevdoc_docname", "so_detail", "against_sales_order",
    "last_scanned_warehouse",
}

# ⭐ NEW: Chunk size for splitting large bulk syncs into multiple background jobs.
CHUNK_SIZE = 300
TARGET_SYNC_LOCK_TTL = 7200


def _target_sync_lock_key(source_doctype):
    return f"external_target_sync_lock:{source_doctype}"


def _acquire_target_sync_lock(source_doctype):
    return bool(
        frappe.cache().redis.set(
            _target_sync_lock_key(source_doctype),
            "1",
            nx=True,
            ex=TARGET_SYNC_LOCK_TTL,
        )
    )


def _release_target_sync_lock(source_doctype):
    frappe.cache().redis.delete(_target_sync_lock_key(source_doctype))


def _enqueue_stock_entry_target_batch(source_doctype, names, batch_index=0):
    if batch_index * CHUNK_SIZE >= len(names):
        return

    frappe.enqueue(
        "emkan_insights.emkan_insights.external_stock_entry_sync._sync_stock_entry_docs_job",
        queue="long",
        job_name=f"se_sync_chunk_{frappe.generate_hash(length=8)}_{batch_index}",
        timeout=1000,
        source_doctype=source_doctype,
        names=names,
        batch_index=batch_index,
    )


# ==========================================================
# SAVEPOINT CONTEXT MANAGER
# ==========================================================

@contextmanager
def savepoint_context(name):
    safe_name = frappe.scrub(name)[:50]
    frappe.db.savepoint(safe_name)
    try:
        yield
    except Exception:
        frappe.db.rollback(save_point=safe_name)
        raise


# ==========================================================
# PARENT-LEVEL LINKED FIELDS on Stock Entry
# ==========================================================
STOCK_ENTRY_PARENT_LINKS = {
    "work_order": "Work Order",
    "purchase_order": "Purchase Order",
    "subcontracting_order": "Subcontracting Order",
    "delivery_note_no": "Delivery Note",
    "sales_invoice_no": "Sales Invoice",
    "pick_list": "Pick List",
    "purchase_receipt_no": "Purchase Receipt",
    "asset_repair": "Asset Repair",
    "bom_no": "BOM",
    "job_card": "Job Card",
    "project": "Project",
    "credit_note": "Stock Entry",
    "outgoing_stock_entry": "Stock Entry",
    "amended_from": "Stock Entry",
    "stock_entry_type": "Stock Entry Type",
    "from_warehouse": "Warehouse",
    "to_warehouse": "Warehouse",
    "supplier": "Supplier",
    "supplier_address": "Address",
    "source_warehouse_address": "Address",
    "target_warehouse_address": "Address",
    "select_print_heading": "Print Heading",
    "letter_head": "Letter Head",
}

COMPANY_PREFIXED_PARENT_FIELDS = {
    "work_order",
    "purchase_order",
    "bom_no",
    "job_card",
    "outgoing_stock_entry",
    "credit_note",
}


# ==========================================================
# CHILD-LEVEL LINKED FIELDS on Stock Entry Detail
# ==========================================================
STOCK_ENTRY_ITEM_LINKS = {
    "s_warehouse": "Warehouse",
    "t_warehouse": "Warehouse",
    "item_code": "Item",
    "quality_inspection": "Quality Inspection",
    "subcontracted_item": "Item",
    "uom": "UOM",
    "stock_uom": "UOM",
    "serial_and_batch_bundle": "Serial and Batch Bundle",
    "batch_no": "Batch",
    "expense_account": "Account",
    "cost_center": "Cost Center",
    "project": "Project",
    "bom_no": "BOM",
    "material_request": "Material Request",
    "material_request_item": "Material Request Item",
    "original_item": "Item",
    "against_stock_entry": "Stock Entry",
    "putaway_rule": "Putaway Rule",
    "reference_purchase_receipt": "Purchase Receipt",
}

COMPANY_PREFIXED_ITEM_FIELDS = {
    "bom_no",
    "against_stock_entry",
    "reference_purchase_receipt",
}

# Fields that, together, are used to detect *genuine* accidental duplicate
# inserts (e.g. a retried sync run). Kept intentionally narrow+specific so
# legitimately distinct rows are never collapsed. See _make_item_fingerprint().
DEDUP_KEY_FIELDS = (
    "item_code",
    "s_warehouse",
    "t_warehouse",
    "qty",
    "batch_no",
    "serial_no",
    "cost_center",
    "expense_account",
    "basic_rate",
    "serial_and_batch_bundle",
)


# ==========================================================
# FORCE CANCEL & SAFE CANCEL HELPERS
# ==========================================================

def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


def _force_cancel_doc(doctype, docname):
    """
    INTENTIONALLY DISABLED: Force-cancelling stock-affecting documents by raw
    SQL is dangerous — it flips docstatus without reversing Stock Ledger Entries
    or GL Entries, leaving phantom inventory and incorrect accounting balances.

    If the ORM cancel() fails, the correct action is to log a CRITICAL error
    and require manual intervention — not to silently corrupt stock data.

    This function is kept as a named stub so call-site references surface
    a clear ValidationError rather than a silent no-op.
    """
    raise frappe.ValidationError(
        f"Force-cancelling stock document '{doctype}' '{docname}' via raw SQL is "
        f"not allowed: it bypasses Stock Ledger Entry and GL Entry reversal, "
        f"creating phantom inventory and accounting drift. "
        f"Investigate why doc.cancel() failed and resolve manually."
    )


def _safe_cancel_doc(doc, log_prefix=""):
    """
    Cancel a document using the ORM (doc.cancel()), which correctly reverses
    Stock Ledger Entries and GL Entries.

    If the ORM cancel fails, the exception is logged as CRITICAL and re-raised.
    Force-cancellation via raw SQL is intentionally NOT used as a fallback
    because it leaves SLEs and GL Entries unreversed (phantom inventory /
    accounting drift).
    """
    try:
        doc.flags.ignore_permissions = True
        doc.cancel()  # ORM cancel — reverses SLE + GL Entries
        _log(f"{log_prefix} cancelled normally", "")
        return True
    except Exception as cancel_err:
        _log(
            f"CRITICAL: {log_prefix} ORM cancel failed — manual intervention required",
            f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
            f"The document has NOT been force-cancelled. Its Stock Ledger Entries "
            f"and GL Entries remain active. Investigate why cancel() failed and "
            f"cancel the document manually via ERPNext to ensure ledger integrity."
        )
        raise


# ==========================================================
# OTHER HELPERS
# ==========================================================

def _get_company_abbr(company):
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(f"Company '{company}' has no abbreviation set")
    return abbr


def _build_target_name(company_abbr, base_id):
    base_id = (base_id or "").strip()
    if not base_id:
        frappe.throw("Missing base id for Stock Entry naming")

    double_prefix = f"{company_abbr}-{company_abbr}-"
    if base_id.startswith(double_prefix):
        return base_id

    return f"{company_abbr}-{base_id}"


def _prefix_reference(company_abbr, value):
    value = str(value or "").strip()
    if not value:
        return None

    if value.startswith(f"{company_abbr}-"):
        return value

    return f"{company_abbr}-{value}"


def _doctype_has_field(doctype, fieldname):
    meta = frappe.get_meta(doctype)
    return meta.has_field(fieldname) or fieldname in meta.get_valid_columns()


def _filter_valid_fields(doctype, values):
    meta_columns = set(frappe.get_meta(doctype).get_valid_columns())
    db_columns = set(frappe.db.get_table_columns(doctype))
    valid_fields = meta_columns & db_columns
    return {k: v for k, v in values.items() if k in valid_fields}


def _resolve_link_with_prefix(doctype, remote_name, company_abbr=None):
    """
    Resolve a linked document reference by trying company-prefixed name first.
    """
    if not remote_name:
        return None

    remote_name = str(remote_name).strip()
    if not remote_name:
        return None

    # 1. Already prefixed
    if company_abbr and remote_name.startswith(f"{company_abbr}-"):
        if frappe.db.exists(doctype, remote_name):
            return remote_name

    # 2. Try prefixed version
    if company_abbr:
        prefixed = f"{company_abbr}-{remote_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    # 3. Try exact match
    if frappe.db.exists(doctype, remote_name):
        return remote_name

    # 4. Try remote_id lookup
    meta = frappe.get_meta(doctype)
    for field in ("remote_id", "custom_remote_id"):
        if meta.has_field(field):
            local_name = frappe.db.get_value(doctype, {field: remote_name}, "name")
            if local_name:
                return local_name

    # 5. Not found
    return None


def _resolve_item_code(item_code):
    """Resolve item code via External Item mapping if needed."""
    if not item_code:
        return item_code

    if frappe.db.exists("Item", item_code):
        return item_code

    # Try External Item mapping
    mapped = frappe.db.get_value("External Item", item_code, "remote_id")
    if mapped:
        return mapped

    external_item_meta = frappe.get_meta("External Item")
    for field in ("remote_id", "custom_remote_id"):
        if external_item_meta.has_field(field):
            mapped_name = frappe.db.get_value("External Item", {field: item_code}, "remote_id")
            if mapped_name:
                return mapped_name

    return item_code


def _resolve_material_request(remote_mr_name, company_abbr=None):
    """
    Same pattern as PO/SQ/RFQ link resolvers:
    1. remote_id / custom_remote_id on Material Request
    2. Double-prefix local name ({abbr}-{remote_name})
    3. Bridge via External Material Request.remote_id
    4. Exact local name
    """
    if not remote_mr_name:
        return None

    remote_mr_name = str(remote_mr_name).strip()
    if not remote_mr_name:
        return None

    candidates = [remote_mr_name]
    if company_abbr and not remote_mr_name.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_mr_name}")

    # 1. remote_id / custom_remote_id (authoritative — MR sync stores remote name here)
    for name in candidates:
        for field in ("remote_id", "custom_remote_id"):
            if _doctype_has_field("Material Request", field):
                found = frappe.db.get_value("Material Request", {field: name}, "name")
                if found:
                    return found

    # 2. Prefixed / exact local name
    for name in candidates:
        if frappe.db.exists("Material Request", name):
            return name

    # 3. Bridge via External Material Request → local MR name
    for name in candidates:
        if frappe.db.exists("External Material Request", name):
            linked = frappe.db.get_value("External Material Request", name, "remote_id")
            if linked and frappe.db.exists("Material Request", linked):
                return linked

    return None


def _remap_material_request_links(stock_entry_name, external, company_abbr):
    """
    Resolve and write local Material Request links on Stock Entry child rows.

    IMPORTANT: This function may only modify child rows when the parent Stock
    Entry is still a Draft (docstatus == 0).  For Submitted or Cancelled
    documents it logs a warning and returns without touching any data — direct
    modification of submitted/cancelled child rows bypasses the ERPNext
    accounting cycle and must never be done here.
    """
    if not stock_entry_name or not external:
        return

    # Guard: only operate on Draft Stock Entries.
    local_docstatus = frappe.db.get_value("Stock Entry", stock_entry_name, "docstatus")
    if cint(local_docstatus) != 0:
        _log(
            f"Stock Entry {stock_entry_name} MR remap skipped — not a Draft",
            f"docstatus={local_docstatus}. Material Request links can only be "
            f"repaired while the document is in Draft state. Submit or Cancelled "
            f"documents must not be modified directly.",
        )
        return

    external_by_item = {}
    if getattr(external, "items", None):
        for row in external.items:
            item_code = _resolve_item_code(getattr(row, "item_code", None))
            remote_mr = getattr(row, "material_request", None)
            if item_code and remote_mr:
                external_by_item[item_code] = remote_mr

    local_rows = frappe.get_all(
        "Stock Entry Detail",
        filters={"parent": stock_entry_name},
        fields=["name", "item_code", "material_request"],
    )

    for local_row in local_rows:
        remote_mr = external_by_item.get(local_row.item_code) or local_row.material_request
        if not remote_mr:
            continue

        resolved_mr = _resolve_material_request(remote_mr, company_abbr)
        if not resolved_mr:
            continue

        mr_item = frappe.db.get_value(
            "Material Request Item",
            {"parent": resolved_mr, "item_code": local_row.item_code},
            "name",
        )

        values = {}
        if resolved_mr != local_row.material_request:
            values["material_request"] = resolved_mr
        if mr_item:
            values["material_request_item"] = mr_item

        if values:
            frappe.db.set_value(
                "Stock Entry Detail",
                local_row.name,
                values,
                update_modified=False,
            )


def _apply_parent_link_mapping(se_doc, external, company_abbr):
    """Resolve and prefix all parent-level linked fields on Stock Entry."""
    for fieldname, linked_doctype in STOCK_ENTRY_PARENT_LINKS.items():
        if not _doctype_has_field("Stock Entry", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            continue

        # Fields that should always be prefixed
        if fieldname in COMPANY_PREFIXED_PARENT_FIELDS:
            se_doc.set(fieldname, _prefix_reference(company_abbr, raw_value))
            continue

        # Other links: try resolve with prefix first
        resolved = _resolve_link_with_prefix(linked_doctype, raw_value, company_abbr)
        if resolved:
            se_doc.set(fieldname, resolved)
        else:
            se_doc.set(fieldname, None)
            frappe.log_error(
                title=f"Stock Entry parent link unresolved: {external.name}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _apply_item_link_mapping(item_row, company_abbr, external_name=None):
    """Resolve and prefix all child-level linked fields on Stock Entry Detail rows."""
    for fieldname, linked_doctype in STOCK_ENTRY_ITEM_LINKS.items():
        raw_value = item_row.get(fieldname)
        if not raw_value:
            continue

        # Fields that should always be prefixed
        if fieldname in COMPANY_PREFIXED_ITEM_FIELDS:
            item_row[fieldname] = _prefix_reference(company_abbr, raw_value)
            continue

        # Item code special handling
        if fieldname == "item_code":
            item_row[fieldname] = _resolve_item_code(raw_value)
            continue

        # Material Request: resolve by remote_id first
        if fieldname == "material_request":
            resolved = _resolve_material_request(raw_value, company_abbr)
            if resolved:
                item_row[fieldname] = resolved
            else:
                item_row[fieldname] = None
                frappe.log_error(
                    title=f"Stock Entry item link unresolved: {external_name or 'Unknown'}",
                    message=f"Field: material_request\nIncoming: {raw_value}\nAbbr: {company_abbr}"
                )
            continue

        # Other links: try resolve with prefix first
        resolved = _resolve_link_with_prefix(linked_doctype, raw_value, company_abbr)
        if resolved:
            item_row[fieldname] = resolved
        else:
            item_row[fieldname] = None
            frappe.log_error(
                title=f"Stock Entry item link unresolved: {external_name or 'Unknown'}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _resolve_material_request_item(item_row):
    """Resolve material_request_item using local parent + item_code."""
    mr_name = item_row.get("material_request")
    item_code = item_row.get("item_code")

    if not mr_name or not item_code:
        return

    mr_item_name = frappe.db.get_value(
        "Material Request Item",
        {"parent": mr_name, "item_code": item_code},
        "name"
    )

    if mr_item_name:
        item_row["material_request_item"] = mr_item_name


def _make_item_fingerprint(item_row):
    """Create a unique fingerprint for deduplication."""
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(item_row.get("item_code") or "").strip(),
        str(item_row.get("s_warehouse") or "").strip(),
        str(item_row.get("t_warehouse") or "").strip(),
        safe_float(item_row.get("qty")),
        str(item_row.get("batch_no") or "").strip(),
        str(item_row.get("serial_no") or "").strip(),
    )


# _deduplicate_child_table() and _safe_dedupe() have been removed.
# Deduplication is now performed BEFORE ORM insert via _make_item_fingerprint()
# inside _build_child_rows(). Post-insert child-table deletion via raw SQL is
# intentionally not done — it would bypass ERPNext's child-row bookkeeping.


def _repair_parent_links(existing_name, external, company_abbr):
    """Rewrite remote parent link values on an existing draft Stock Entry."""
    updates = {}

    for fieldname, linked_doctype in STOCK_ENTRY_PARENT_LINKS.items():
        if not _doctype_has_field("Stock Entry", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            updates[fieldname] = None
            continue

        if fieldname in COMPANY_PREFIXED_PARENT_FIELDS:
            updates[fieldname] = _prefix_reference(company_abbr, raw_value)
            continue

        updates[fieldname] = _resolve_link_with_prefix(
            linked_doctype, raw_value, company_abbr
        )

    if updates:
        frappe.db.set_value(
            "Stock Entry",
            existing_name,
            _filter_valid_fields("Stock Entry", updates),
            update_modified=False,
        )


# ==========================================================
# HANDLE EXISTING DOC STATUS SYNC
# ==========================================================

def _sync_existing_docstatus(existing_name, external, company_abbr=None):
    """
    Handle docstatus sync for already-existing Stock Entry documents.

    Cancel path strictly uses ORM doc.cancel() to ensure Stock Ledger Entries
    and GL Entries are properly reversed. Force-cancellation via raw SQL is
    intentionally not used — see _safe_cancel_doc() for rationale.
    """
    local_docstatus = frappe.db.get_value("Stock Entry", existing_name, "docstatus")
    target_docstatus = cint(external.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists"}

    try:
        if cint(local_docstatus) == 0 and company_abbr:
            _repair_parent_links(existing_name, external, company_abbr)
            frappe.clear_document_cache("Stock Entry", existing_name)

        se = frappe.get_doc("Stock Entry", existing_name)
        se.flags.ignore_permissions = True

        if target_docstatus == 2:
            # External is cancelled — drive the local doc to cancelled too.
            if local_docstatus == 0:
                # Draft locally → must submit first so ERPNext can cancel normally
                # (cancel() requires docstatus=1). If submit fails we log the
                # error and return — the doc stays as a draft flagged for review.
                # We do NOT force-cancel from Draft: a draft with no SLE/GL
                # entries yet can simply be deleted manually if not needed.
                try:
                    se.submit()  # ORM submit — creates SLE + GL entries
                    se = frappe.get_doc("Stock Entry", existing_name)
                except Exception as submit_err:
                    _log(
                        f"CRITICAL: Stock Entry {existing_name} submit-before-cancel failed "
                        f"— document left in Draft for manual review",
                        f"Error: {str(submit_err)}\n\n{frappe.get_traceback()}\n\n"
                        f"The document has NOT been cancelled. Resolve the submit "
                        f"failure manually, then cancel via ERPNext."
                    )
                    return {
                        "name": existing_name,
                        "status": "exists",
                        "warning": "submit-before-cancel failed; left in Draft — manual intervention required",
                        "error": str(submit_err),
                    }

            # Now cancel via ORM — this reverses SLE + GL entries correctly.
            # _safe_cancel_doc() will raise if cancel() fails; the exception
            # propagates to savepoint_context which rolls back the transaction.
            _safe_cancel_doc(se, log_prefix=f"Stock Entry {existing_name}")

            # Verify
            final_docstatus = frappe.db.get_value("Stock Entry", existing_name, "docstatus")
            if final_docstatus == 2:
                return {"name": existing_name, "status": "docstatus_synced", "docstatus": 2}
            else:
                _log(
                    f"Stock Entry {existing_name} cancel verification failed",
                    f"Expected docstatus=2, got {final_docstatus}"
                )
                return {"name": existing_name, "status": "exists", "warning": "cancel verification failed"}

        elif target_docstatus == 1 and local_docstatus == 0:
            se.submit()  # ORM submit writes to DB
            return {"name": se.name, "status": "docstatus_synced", "docstatus": 1}

        else:
            _log(
                f"Stock Entry {existing_name} unhandled docstatus transition",
                f"local={local_docstatus}, external={target_docstatus}"
            )
            return {"name": existing_name, "status": "exists", "warning": "unhandled transition"}

    except Exception as trans_err:
        _log(f"Stock Entry {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(trans_err)}

# ==========================================================
# BUILD HEADER VALUES FOR DIRECT SQL INSERT
# ==========================================================

def _build_header_values(external, company, company_abbr):
    """Build a dict of valid header field values for direct SQL insert."""
    header = {}

    # Copy simple fields (exclude child tables, system fields, link fields)
    source_data = external.as_dict()
    for field, value in source_data.items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ("items", "additional_costs", "remote_id", "stock_entry_type"):
            continue
        if field in STOCK_ENTRY_PARENT_LINKS:
            continue
        if not _doctype_has_field("Stock Entry", field):
            continue
        header[field] = value

    # Override company
    header["company"] = company

    # Resolve parent-level links with company prefix
    for fieldname, linked_doctype in STOCK_ENTRY_PARENT_LINKS.items():
        if not _doctype_has_field("Stock Entry", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            header[fieldname] = None
            continue

        if fieldname in COMPANY_PREFIXED_PARENT_FIELDS:
            header[fieldname] = _prefix_reference(company_abbr, raw_value)
            continue

        resolved = _resolve_link_with_prefix(linked_doctype, raw_value, company_abbr)
        if resolved:
            header[fieldname] = resolved
        else:
            header[fieldname] = None

    # Set remote_id to the EXTERNAL name (not the local name)
    if _doctype_has_field("Stock Entry", "remote_id"):
        header["remote_id"] = (external.remote_id or external.name or "").strip()

    if _doctype_has_field("Stock Entry", "source_site"):
        header["source_site"] = getattr(external, "source_site", None)

    # Filter to only valid columns
    header = _filter_valid_fields("Stock Entry", header)

    return header


# ==========================================================
# BUILD CHILD ROW VALUES
# ==========================================================

def _build_child_rows(external, company_abbr):
    """Build deduplicated child row dicts for Stock Entry Detail."""
    rows = []
    seen = set()

    if not hasattr(external, "items") or not external.items:
        return rows

    for row in external.items:
        item_row = {}
        for field, value in row.as_dict().items():
            if field in SYSTEM_FIELDS:
                continue
            if field in IGNORE_ITEM_FIELDS:
                continue
            item_row[field] = value

        # Resolve all linked fields with company prefix
        _apply_item_link_mapping(item_row, company_abbr, external.name)

        # Resolve material_request_item if material_request was resolved
        _resolve_material_request_item(item_row)

        # Filter to only valid fields
        item_row = _filter_valid_fields("Stock Entry Detail", item_row)

        # Deduplicate
        fingerprint = _make_item_fingerprint(item_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        rows.append(item_row)

    return rows


def _build_additional_cost_rows(external):
    """Build child row dicts for Landed Cost Taxes and Charges."""
    rows = []

    if not hasattr(external, "additional_costs") or not external.additional_costs:
        return rows

    for row in external.additional_costs:
        cost_row = {}
        for field, value in row.as_dict().items():
            if field in SYSTEM_FIELDS:
                continue
            cost_row[field] = value

        cost_row = _filter_valid_fields("Landed Cost Taxes and Charges", cost_row)
        rows.append(cost_row)

    return rows


# ==========================================================
# ORM-BASED INSERT HELPER (NO COMMIT)
# ==========================================================

def _direct_insert_stock_entry(target_name, header_values, item_rows, cost_rows):
    """
    Insert Stock Entry and child tables via the Frappe ORM (frappe.new_doc /
    doc.insert()) instead of raw SQL INSERT statements.

    This ensures the Stock Entry controller's full lifecycle runs on insert:
      - validate() / set_stock_entry_type()
      - calculate_rate_and_amount() (stock_qty, basic_amount, etc.)
      - conversion_factor → stock_qty derivation
      - warehouse / work-order / BOM validations

    Rate preservation strategy:
      1. Snapshot remote basic_rate / basic_amount / amount / valuation_rate
         per row BEFORE insert (the controller will overwrite these with local
         stock ledger values).
      2. Set set_basic_rate_manually=1 on each row (ERPNext flag that tells
         set_basic_rate() to skip rate recalc for incoming/finished items).
      3. After doc.insert(), compare DB values to snapshot and issue a targeted
         SQL UPDATE to restore any rows the controller changed. This also covers
         outgoing items, which set_basic_rate_manually does NOT protect.
      4. Recompute parent-level total_outgoing_value / total_incoming_value
         from the corrected child rows.

    No savepoints, no rollback calls — relies on Frappe's automatic
    transaction rollback when the exception propagates.
    """

    # ── Collision check ──────────────────────────────────────────
    if frappe.db.exists("Stock Entry", target_name):
        raise frappe.ValidationError(
            _("Stock Entry {0} already exists. Cannot create duplicate.").format(target_name)
        )

    # ── Build parent doc via ORM ─────────────────────────────────
    # Assign an explicit deterministic name (bypasses naming_series/autoname)
    # while letting everything else go through the normal Document lifecycle.
    doc = frappe.new_doc("Stock Entry")
    doc.name = target_name
    doc.flags.name_set = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_naming_series = True

    for field, value in header_values.items():
        doc.set(field, value)

    # ── Append children: Stock Entry Detail ──────────────────────
    # set_basic_rate_manually=1 signals the ERPNext controller to skip
    # automatic rate recalculation for incoming/finished items so that
    # the remote rates are honoured as-is by validate().
    for item_row in item_rows:
        item_row["set_basic_rate_manually"] = 1
        doc.append("items", item_row)

    # ── Append children: Landed Cost Taxes and Charges ───────────
    for cost_row in cost_rows:
        doc.append("additional_costs", cost_row)

    # insert() runs the full Stock Entry controller lifecycle:
    # validate(), set_stock_entry_type(), calculate_rate_and_amount(),
    # stock_qty derivation from conversion_factor, warehouse/WO validations.
    # We do NOT patch rate fields after insert — the ORM-computed values
    # are authoritative. set_basic_rate_manually above is the only hint.
    doc.insert(ignore_permissions=True)

    return doc.name


# ==========================================================
# DOUBLE-COUNT RISK GUARD (CRITICAL-5)
# ==========================================================

def _get_company_opening_sr_cutoff(company):
    """
    Return the posting_date of the *earliest* submitted Stock Reconciliation
    for this company, or None if no SR exists.

    This is the Strategy-B cutoff: Stock Entries posted on or before this
    date must not be synced because they are already captured in the opening
    Stock Reconciliation balance.
    """
    result = frappe.db.sql(
        """
        SELECT MIN(posting_date) AS cutoff
        FROM `tabStock Reconciliation`
        WHERE company   = %s
          AND docstatus = 1
        """,
        (company,),
        as_dict=True,
    )
    if result and result[0].cutoff:
        return result[0].cutoff
    return None


def _enforce_strategy_b_cutoff(se_name, company, posting_date):
    """
    Strategy B (CRITICAL-5): Block sync of a Stock Entry whose posting_date
    falls on or before the earliest submitted Stock Reconciliation for this
    company.

    Rationale: the opening Stock Reconciliation sets absolute on-hand
    quantities that already incorporate all prior transactions.  Syncing
    historical Stock Entries that pre-date (or land on) the SR date would
    double-count those quantities.

    Raises frappe.ValidationError (which savepoint_context rolls back) so the
    document is never inserted and a clear audit trail is written to the error
    log.
    """
    if not posting_date:
        return  # No date — let the controller validate

    cutoff = _get_company_opening_sr_cutoff(company)
    if not cutoff:
        return  # No opening SR for this company — nothing to guard against

    if posting_date <= cutoff:
        msg = (
            f"Strategy B cutoff enforced: Stock Entry '{se_name}' "
            f"(posting_date={posting_date}) was NOT synced because it falls "
            f"on or before the earliest opening Stock Reconciliation for "
            f"company '{company}' (SR cutoff date: {cutoff}). "
            f"These quantities are already included in the opening balance. "
            f"To sync this entry, either (a) cancel the opening SR and switch "
            f"to Strategy A (full historical reconstruction), or (b) confirm "
            f"this entry should not be imported."
        )
        _log(f"Strategy B cutoff: skipping Stock Entry {se_name}", msg)
        raise frappe.ValidationError(msg)


def _detect_submitted_item_mismatch(existing_name, external):
    """
    Surface remote item changes on an already-submitted local Stock Entry.

    Submitted/cancelled documents cannot be rebuilt here without reversing and
    recreating Stock Ledger Entries, so we do NOT modify them. We only log a
    clear CRITICAL warning so the discrepancy is visible for manual review
    instead of being silently skipped (docstatus already matches).
    """
    local_docstatus = cint(frappe.db.get_value("Stock Entry", existing_name, "docstatus"))
    if local_docstatus == 0:
        return  # Draft — rebuildable / handled elsewhere; nothing to flag

    external_items = len(external.items or [])
    if external_items == 0:
        return  # Cannot reliably detect (external items may not have loaded)

    local_items = frappe.db.count("Stock Entry Detail", {"parent": existing_name})

    if external_items != local_items:
        _log(
            f"Stock Entry {existing_name} item mismatch on submitted doc",
            f"External Stock Entry has {external_items} item row(s) but local "
            f"Stock Entry has {local_items}. Local docstatus={local_docstatus} — "
            f"the document is already submitted/cancelled, so it is NOT rebuilt. "
            f"Manual review required to reconcile the item difference."
        )


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_stock_entry(external_name, company):
    external = frappe.get_doc("External Stock Entry", external_name)

    company_abbr = _get_company_abbr(company)
    source_stock_entry_id = (external.remote_id or external.name or "").strip()
    target_name = _build_target_name(company_abbr, source_stock_entry_id)

    existing = frappe.db.get_value(
        "Stock Entry", {"remote_id": source_stock_entry_id}, "name"
    ) or (target_name if frappe.db.exists("Stock Entry", target_name) else None)

    # ── Cancelled external with NO local copy: nothing to cancel, skip ──
    if cint(external.docstatus) == 2 and not existing:
        _log(
            f"Stock Entry cancelled external skip: {external.name}",
            "External is cancelled and no local copy exists — nothing to sync."
        )
        return {
            "name": external_name,
            "status": "skipped",
            "reason": "Cancelled external with no local copy"
        }

    if existing:
        # Existing SE path: sync docstatus only.
        # _remap_material_request_links() is guarded internally — it will only
        # modify child rows when the Stock Entry is still a Draft (docstatus=0).
        try:
            _detect_submitted_item_mismatch(existing, external)
        except Exception:
            _log(
                f"Stock Entry {existing} item mismatch detection failed",
                frappe.get_traceback(),
            )
        try:
            _remap_material_request_links(existing, external, company_abbr)
        except Exception:
            _log(
                f"Stock Entry {existing} material_request remap failed",
                frappe.get_traceback(),
            )
        return _sync_existing_docstatus(existing, external, company_abbr)

    # ==========================================================
    # CREATE PATH: ORM insert with an explicit deterministic name
    # (bypasses naming_series only — all controller validation runs)
    # ==========================================================

    # Build header values
    header_values = _build_header_values(external, company, company_abbr)

    # Strategy B cutoff check (CRITICAL-5): reject this Stock Entry if its
    # posting_date falls on or before the earliest submitted Stock
    # Reconciliation for this company. Raises ValidationError if blocked.
    _enforce_strategy_b_cutoff(
        se_name=target_name,
        company=company,
        posting_date=header_values.get("posting_date"),
    )

    # Build child rows
    item_rows = _build_child_rows(external, company_abbr)
    cost_rows = _build_additional_cost_rows(external)

    # Log pre-insert state
    _log(
        f"Stock Entry {target_name} pre-insert check",
        f"Items: {len(item_rows)} (remote had {len(external.items or [])}), "
        f"Stock Entry Type: {header_values.get('stock_entry_type')}, "
        f"Company: {company}, External docstatus: {external.docstatus}"
    )

    # ORM insert — runs full Stock Entry controller lifecycle
    _direct_insert_stock_entry(target_name, header_values, item_rows, cost_rows)

    # Remap MR links immediately after insert (document is still Draft here).
    try:
        _remap_material_request_links(target_name, external, company_abbr)
    except Exception:
        _log(
            f"Stock Entry {target_name} material_request post-insert remap failed",
            frappe.get_traceback(),
        )

    _log(f"Stock Entry {target_name} inserted successfully", "Docstatus after insert: 0")

    # ==========================================================
    # DOCSTATUS SYNC
    # ==========================================================
    target_docstatus = cint(external.docstatus)

    frappe.clear_document_cache("Stock Entry", target_name)
    se = frappe.get_doc("Stock Entry", target_name)

    _log(
        f"Stock Entry {se.name} pre-docstatus-sync",
        f"External docstatus: {target_docstatus}, Current docstatus: {se.docstatus}"
    )

    if target_docstatus == 1:
        # Submit the Stock Entry
        try:
            se.flags.ignore_permissions = True
            se.set_posting_time = 1
            se.submit()  # ORM submit — creates SLE + GL entries
            _log(f"Stock Entry {se.name} submitted successfully", "")

            actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
            return {
                "name": se.name,
                "status": "synced",
                "docstatus": 1,
                "items_count": actual_item_count
            }

        except Exception as submit_err:
            tb = frappe.get_traceback()
            _log(
                f"Stock Entry {se.name} SUBMIT FAILED — left in Draft",
                f"Error: {str(submit_err)}\n\nTraceback:\n{tb}\n\n"
                f"The document was inserted as a Draft. Review and submit manually "
                f"or investigate the submit failure before retrying sync."
            )
            actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
            return {
                "name": se.name,
                "status": "synced_draft",
                "error": f"Submit failed: {str(submit_err)}",
                "items_count": actual_item_count
            }

    else:
        # docstatus = 0 (Draft) — already in correct state
        actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
        return {
            "name": se.name,
            "status": "synced",
            "docstatus": 0,
            "items_count": actual_item_count
        }


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_stock_entry_docs(source_doctype=None, names=None):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order. This guarantees
    # the same record can never end up in two different chunks, which is the
    # only scenario that could cause a duplicate Stock Entry to be created
    # when chunks run in parallel on different workers.
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    if not unique_names:
        return "No External Stock Entry records pending sync"

    if not _acquire_target_sync_lock(source_doctype or "External Stock Entry"):
        return f"{source_doctype or 'External Stock Entry'} target sync is already running"

    _enqueue_stock_entry_target_batch(
        source_doctype or "External Stock Entry",
        unique_names,
        batch_index=0,
    )

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into sequential "
        f"batches of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER (unchanged — processes whatever chunk it receives)
# ==========================================================
def _sync_stock_entry_docs_job(source_doctype=None, names=None, batch_index=0):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    batch_index = int(batch_index or 0)
    chunk = names[batch_index * CHUNK_SIZE:(batch_index + 1) * CHUNK_SIZE]
    results = []

    for name in chunk:
        try:
            with sync_record_guard(source_doctype or "External Stock Entry", name):
                external_doc = frappe.get_doc(source_doctype or "External Stock Entry", name)
                sync_started_at = external_doc.modified

                result = sync_external_stock_entry(name, external_doc.company)
                results.append(result)

                if result.get("status") in ("synced", "exists", "docstatus_synced", "skipped"):
                    frappe.db.set_value(
                        source_doctype or "External Stock Entry",
                        name,
                        "last_synced_on",
                        sync_started_at,
                        update_modified=False
                    )

                frappe.db.commit()

        except Exception as e:
            frappe.db.rollback()
            frappe.log_error(
                title=f"Stock Entry Sync Error: {name}",
                message=frappe.get_traceback()
            )
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    next_batch_index = batch_index + 1
    if next_batch_index * CHUNK_SIZE < len(names):
        _enqueue_stock_entry_target_batch(
            source_doctype or "External Stock Entry",
            names,
            next_batch_index,
        )
    else:
        _release_target_sync_lock(source_doctype or "External Stock Entry")

    return results
