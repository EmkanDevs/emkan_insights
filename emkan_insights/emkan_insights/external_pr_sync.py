import frappe
import json
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.sync_guard import (
    acquire_sync_lock,
    release_sync_lock,
    set_source_site_fields,
)

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

# ⭐ FIX: Added purchase_invoice_item and purchase_receipt return-link fields to ignore list
IGNORE_ITEM_FIELDS = {
    "purchase_order_item",
    "purchase_invoice_item",
    "purchase_receipt_item",
    "purchase_receipt_detail",
    "prevdoc_doctype",
    "prevdoc_docname"
}

# ⭐ NEW: Chunk size for splitting large bulk syncs into multiple background jobs.
# 300 keeps each job comfortably under the 2000s timeout even when records need
# submit() calls, negative-stock retries, or several link-resolution lookups.
CHUNK_SIZE = 300


# def _resolve_default_warehouse(company):
#     if not company:
#         return None
#     return frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")


def _get_table_fieldnames(meta):
    return {df.fieldname for df in meta.get_table_fields()}


def _apply_sync_flags(doc):
    """Apply all necessary flags for sync operations"""
    doc.flags.ignore_permissions = True
    doc.flags.ignore_naming_series = True
    #frappe.flags.ignore_stock_validation = True


def _get_source_docstatus(ext_doc):
    """Safely get docstatus from source document."""
    docstatus = ext_doc.get("docstatus", 0)

    if isinstance(docstatus, str):
        try:
            docstatus = int(docstatus)
        except (ValueError, TypeError):
            docstatus = 0

    if not isinstance(docstatus, int) or docstatus not in (0, 1, 2):
        status = ext_doc.get("status", "")
        if isinstance(status, str):
            status_lower = status.lower().strip()
            if status_lower == "submitted":
                docstatus = 1
            elif status_lower == "cancelled":
                docstatus = 2
            else:
                docstatus = 0
        else:
            docstatus = 0

    return docstatus


def _copy_child_row_fields(row, target_child_doctype, extra_ignore=None):
    target_meta = frappe.get_meta(target_child_doctype)
    target_table_fields = _get_table_fieldnames(target_meta)
    ignored_fields = set(extra_ignore or [])
    row_data = {"doctype": target_child_doctype}

    for field, value in row.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ignored_fields:
            continue
        if field in target_table_fields:
            continue
        if target_meta.has_field(field):
            row_data[field] = value

    return row_data


def _append_matching_child_table(target_doc, source_doc, table_fieldname):
    target_df = target_doc.meta.get_field(table_fieldname)
    if not target_df or not target_df.options:
        return

    source_rows = source_doc.get(table_fieldname) or []
    if not source_rows:
        return

    target_doc.set(table_fieldname, [])
    for row in source_rows:
        child_row = _copy_child_row_fields(row, target_df.options)
        if child_row.get("account_head"):
            company_abbr = frappe.db.get_value("Company", target_doc.company, "abbr") or ""
            child_row["account_head"] = resolve_company_account(
                child_row["account_head"], target_doc.company, company_abbr
            )
        target_doc.append(
            table_fieldname,
            child_row
        )


def clean_duplicate_pr_items(parent_name):
    frappe.db.sql("""
        DELETE t1 FROM `tabPurchase Receipt Item` t1
        INNER JOIN `tabPurchase Receipt Item` t2
        WHERE
            t1.name > t2.name
            AND t1.parent = t2.parent
            AND t1.parenttype = t2.parenttype
            AND t1.parenttype = 'Purchase Receipt'
            AND t1.parent = %s
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
    """, (parent_name,))


# =============================================================================
# RESOLUTION HELPERS
# =============================================================================
def _is_cancelled(doctype, name):
    """Return True if the given document exists and is cancelled (docstatus == 2)."""
    if not name:
        return False
    return frappe.db.get_value(doctype, name, "docstatus") == 2


def resolve_local_po(remote_or_local_po, company_abbr=None):
    if not remote_or_local_po:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Purchase Order
        return None if _is_cancelled("Purchase Order", name) else name

    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return _valid(prefixed)
    local_name = frappe.db.get_value("Purchase Order", {"remote_id": remote_or_local_po}, "name")
    if local_name:
        return _valid(local_name)
    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return _valid(remote_or_local_po)
    return None


def get_po_detail(purchase_order, item_code, remote_po_item=None):
    if not purchase_order:
        return None
    if remote_po_item:
        if frappe.db.exists("Purchase Order Item", {"name": remote_po_item, "parent": purchase_order}):
            return remote_po_item
        poi_meta = frappe.get_meta("Purchase Order Item")
        if poi_meta.has_field("custom_remote_id"):
            local_po_item = frappe.db.get_value("Purchase Order Item", {"parent": purchase_order, "custom_remote_id": remote_po_item}, "name")
            if local_po_item:
                return local_po_item
    return frappe.db.get_value("Purchase Order Item", {"parent": purchase_order, "item_code": item_code}, "name")


def sync_item_with_po_reference(item_row):
    """
    Keep PR item fields aligned with the linked Purchase Order Item.
    ERPNext validates these fields against the previous document during insert.
    """
    po_item = item_row.get("purchase_order_item")
    if not po_item:
        return

    po_values = frappe.db.get_value(
        "Purchase Order Item",
        po_item,
        ["project", "uom", "item_code"],
        as_dict=True,
    )
    if not po_values:
        return

    item_row["project"] = po_values.get("project")
    if po_values.get("uom"):
        item_row["uom"] = po_values.get("uom")
    if po_values.get("item_code"):
        item_row["item_code"] = po_values.get("item_code")


def resolve_local_mr(remote_or_local_mr, company_abbr=None):
    if not remote_or_local_mr:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Material Request
        return None if _is_cancelled("Material Request", name) else name

    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return _valid(prefixed)
    local_name = frappe.db.get_value("Material Request", {"remote_id": remote_or_local_mr}, "name")
    if local_name:
        return _valid(local_name)
    if frappe.db.exists("Material Request", remote_or_local_mr):
        return _valid(remote_or_local_mr)
    return None


def get_mr_detail(material_request, item_code):
    if not material_request:
        return None
    return frappe.db.get_value("Material Request Item", {"parent": material_request, "item_code": item_code}, "name")


def resolve_local_pi(remote_or_local_pi, company_abbr=None):
    if not remote_or_local_pi:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Purchase Invoice
        return None if _is_cancelled("Purchase Invoice", name) else name

    if company_abbr and not remote_or_local_pi.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pi}"
        if frappe.db.exists("Purchase Invoice", prefixed):
            return _valid(prefixed)
    pi_meta = frappe.get_meta("Purchase Invoice")
    for candidate in ("custom_remote_id", "remote_id"):
        if pi_meta.has_field(candidate):
            local_name = frappe.db.get_value("Purchase Invoice", {candidate: remote_or_local_pi}, "name")
            if local_name:
                return _valid(local_name)
            break
    if frappe.db.exists("Purchase Invoice", remote_or_local_pi):
        return _valid(remote_or_local_pi)
    return None


def get_pi_detail(purchase_invoice, item_code, remote_pi_item=None):
    if not purchase_invoice:
        return None
    if remote_pi_item:
        if frappe.db.exists("Purchase Invoice Item", {"name": remote_pi_item, "parent": purchase_invoice}):
            return remote_pi_item
        pii_meta = frappe.get_meta("Purchase Invoice Item")
        if pii_meta.has_field("custom_remote_id"):
            local_pi_item = frappe.db.get_value("Purchase Invoice Item", {"parent": purchase_invoice, "custom_remote_id": remote_pi_item}, "name")
            if local_pi_item:
                return local_pi_item
    return frappe.db.get_value("Purchase Invoice Item", {"parent": purchase_invoice, "item_code": item_code}, "name")


def resolve_local_pr_for_item(remote_or_local_pr, company_abbr=None):
    if not remote_or_local_pr:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Purchase Receipt
        return None if _is_cancelled("Purchase Receipt", name) else name

    if company_abbr and not remote_or_local_pr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pr}"
        if frappe.db.exists("Purchase Receipt", prefixed):
            return _valid(prefixed)
    pr_meta = frappe.get_meta("Purchase Receipt")
    for candidate in ("custom_remote_id", "remote_id"):
        if pr_meta.has_field(candidate):
            local_name = frappe.db.get_value("Purchase Receipt", {candidate: remote_or_local_pr}, "name")
            if local_name:
                return _valid(local_name)
            break
    if frappe.db.exists("Purchase Receipt", remote_or_local_pr):
        return _valid(remote_or_local_pr)
    return None


def get_pr_detail_for_item(purchase_receipt, item_code, remote_pr_item=None):
    """
    Get the LOCAL Purchase Receipt Item name for a return reference.
    
    IMPORTANT: This function NEVER returns the remote_pr_item value directly.
    It always looks up and returns the actual local database name.
    """
    if not purchase_receipt:
        return None
    
    local_item_name = None
    
    # Strategy 1: Find by custom_remote_id (most accurate for synced data)
    if remote_pr_item:
        pri_meta = frappe.get_meta("Purchase Receipt Item")
        if pri_meta.has_field("custom_remote_id"):
            local_item_name = frappe.db.get_value(
                "Purchase Receipt Item",
                {"parent": purchase_receipt, "custom_remote_id": remote_pr_item},
                "name"
            )
    
    # Strategy 2: Fallback to finding by item_code
    if not local_item_name and item_code:
        # Try exact match first
        local_item_name = frappe.db.get_value(
            "Purchase Receipt Item",
            {"parent": purchase_receipt, "item_code": item_code},
            "name"
        )
        
        # If no exact match, try case-insensitive match
        if not local_item_name:
            local_item_name = frappe.db.sql("""
                SELECT name FROM `tabPurchase Receipt Item`
                WHERE parent = %s AND LOWER(item_code) = LOWER(%s)
                LIMIT 1
            """, (purchase_receipt, item_code))
            if local_item_name:
                local_item_name = local_item_name[0][0]
    
    # SECURITY CHECK: Never return the remote_pr_item value
    # If somehow we got the remote ID, treat it as invalid
    if local_item_name and local_item_name == remote_pr_item:
        # Verify this is actually a valid local item (not just the remote ID leaking through)
        is_valid = frappe.db.exists(
            "Purchase Receipt Item", 
            {"name": local_item_name, "parent": purchase_receipt}
        )
        if not is_valid:
            local_item_name = None
    
    return local_item_name


def _set_docstatus_directly(doctype, docname, docstatus):
    """
    INTENTIONALLY DISABLED: Force-setting docstatus via raw SQL is dangerous —
    for cancels it bypasses Stock Ledger Entry and GL Entry reversal (phantom
    inventory / accounting drift); for submits it bypasses SLE/GL creation
    (document appears Submitted but has zero stock/accounting effect).

    Use _safe_cancel_doc() (ORM cancel) instead.
    This stub is kept so any remaining references surface a clear error
    rather than silently corrupting stock data.
    """
    raise frappe.ValidationError(
        f"_set_docstatus_directly('{doctype}', '{docname}', {docstatus}) is "
        f"not allowed: raw SQL docstatus changes bypass Stock Ledger Entry "
        f"and GL Entry creation/reversal. Use the ORM submit() / cancel() path."
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
        return True
    except Exception as cancel_err:
        frappe.log_error(
            title=f"CRITICAL: {log_prefix} ORM cancel failed — manual intervention required",
            message=(
                f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
                f"The document has NOT been force-cancelled. Its Stock Ledger Entries "
                f"and GL Entries remain active. Investigate why cancel() failed and "
                f"cancel the document manually via ERPNext to ensure ledger integrity."
            )
        )
        raise


# =============================================================================
# ENTRY POINT (WHITELIST)
# =============================================================================
@frappe.whitelist()
@filter_kwargs
def sync_external_purchase_receipt_docs(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    # ⭐ NEW: De-duplicate incoming names while preserving order. This guarantees
    # the same record can never end up in two different chunks, which is the
    # only scenario that could cause a duplicate Purchase Receipt to be created
    # when chunks run in parallel on different workers.
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    # ⭐ NEW: Split into chunks and enqueue one background job per chunk instead
    # of a single job handling all 5000+ records serially. Each job gets a
    # unique job_name so chunks can be tracked/deduped in the RQ job list.
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"pr_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_pr_sync.sync_bulk_purchase_receipts",
            queue="long",
            job_name=job_name,
            names=chunk,
            timeout=2000
        )
        queued_chunks += 1

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
    )


# =============================================================================
# BACKGROUND WORKER
# =============================================================================
def _validate_or_clear_return_link(pr, ext_pr):
    """
    Validate that all return item references are valid before insert.
    Clears return links if any references are invalid.
    """
    if not pr.get("is_return") or not pr.get("return_against"):
        return

    # Get all valid item names AND item_codes from the target PR
    target_items = frappe.get_all(
        "Purchase Receipt Item",
        filters={"parent": pr.return_against},
        fields=["name", "item_code"],
    )
    
    if not target_items:
        # Target PR has no items - can't be a valid return
        frappe.log_error(
            title=f"PR Sync: return_against PR has no items: {pr.return_against}",
            message=f"Cannot create return PR because target {pr.return_against} has no items. Dropping return link."
        )
        pr.return_against = None
        if pr.meta.has_field("is_return"):
            pr.is_return = 0
        return

    # Build lookup structures
    valid_item_names = {r.name for r in target_items}
    item_code_to_row = {}
    for r in target_items:
        key = (r.item_code or "").strip().lower()
        # Keep first match for each item_code
        if key not in item_code_to_row:
            item_code_to_row[key] = r

    has_invalid_ref = False
    items_to_fix = []
    link_field = "purchase_receipt_item"

    for d in pr.items:
        item_needs_fix = False
        
        # Check 1: Validate purchase_receipt_item if set
        if d.get(link_field):
            pr_detail = d.get(link_field)
            
            # Check if it's a valid local item name
            if pr_detail not in valid_item_names:
                # This is likely a remote ID that leaked through - try to fix
                key = (d.item_code or "").strip().lower()
                ref_row = item_code_to_row.get(key)
                
                if ref_row:
                    items_to_fix.append((d, ref_row.name))
                else:
                    item_needs_fix = True
                    frappe.log_error(
                        title=f"PR Sync: invalid purchase_receipt_item '{pr_detail}'",
                        message=(
                            f"Item '{d.item_code}' references non-existent PR item '{pr_detail}' "
                            f"in {pr.return_against}. No matching item_code found either."
                        )
                    )
        
        # Check 2: Validate item_code exists in target
        if not item_needs_fix and d.get("item_code"):
            key = (d.item_code or "").strip().lower()
            canonical = item_code_to_row.get(key)
            
            if canonical is None:
                item_needs_fix = True
                frappe.log_error(
                    title=f"PR Sync: item_code not found in return_against",
                    message=f"Item '{d.item_code}' does not exist in {pr.return_against}"
                )

        if item_needs_fix:
            has_invalid_ref = True

    # Apply fixes for items that can be corrected
    for d, correct_pr_detail in items_to_fix:
        d.purchase_receipt_item = correct_pr_detail
        # Also ensure purchase_receipt is set correctly
        d.purchase_receipt = pr.return_against

    # If we have unfixable references, drop the return link entirely
    if has_invalid_ref:
        frappe.log_error(
            title=f"PR Sync: clearing return link for {pr.name or ext_pr.name}",
            message=(
                f"return_against={pr.return_against}\n"
                f"Item codes on this document: {[d.item_code for d in pr.items]}\n"
                f"Valid item codes in return_against: {sorted(item_code_to_row.keys())}\n\n"
                f"Dropping return_against/is_return so the receipt can still sync as a normal receipt. "
                f"Please review manually."
            ),
        )
        pr.return_against = None
        if pr.meta.has_field("is_return"):
            pr.is_return = 0
        # Also clear item-level return references
        for d in pr.items:
            d.purchase_receipt = None
            d.purchase_receipt_item = None


def _ensure_no_remote_ids_in_return_refs(pr):
    """
    Safety net: Ensure purchase_receipt_item points to the local child-row name.
    This should never happen if the above functions work correctly, but we check
    anyway to prevent validation errors.
    """
    if not pr.get("is_return"):
        return
    
    return_against = pr.get("return_against")
    if not return_against:
        return
    
    # Get all valid item names from the target PR
    valid_names = set(frappe.get_all(
        "Purchase Receipt Item",
        filters={"parent": return_against},
        pluck="name"
    ))
    
    for d in pr.items:
        remote_pr_item = d.get("purchase_receipt_item")
        if remote_pr_item and remote_pr_item in valid_names:
            d.purchase_receipt = return_against
            continue

        local_name = get_pr_detail_for_item(return_against, d.item_code, remote_pr_item)
        if local_name:
            d.purchase_receipt_item = local_name
            d.purchase_receipt = return_against
        else:
            # Can't fix - clear the return reference for this item
            d.purchase_receipt = None
            d.purchase_receipt_item = None
            
def sync_bulk_purchase_receipts(names):
    results = []
    pending_submit_retry = []

    for name in names:
        savepoint = f"pr_sync_{frappe.generate_hash(length=10)}"
        _lock_name = None

        try:
            # ⭐ Per-record advisory lock: prevents two parallel workers from
            # processing the same External Purchase Receipt simultaneously,
            # which would otherwise cause duplicate PR creation or race conditions.
            # lock_timeout=0 means fail immediately if another worker holds the lock.
            _lock_name = acquire_sync_lock("External Purchase Receipt", name, lock_timeout=0)
            frappe.db.savepoint(savepoint)

            ext_pr = frappe.get_doc("External Purchase Receipt", name)
            sync_started_at = ext_pr.modified
            remote_id = ext_pr.name
            source_docstatus = _get_source_docstatus(ext_pr)
            company_abbr = frappe.db.get_value('Company', ext_pr.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Purchase Receipt Sync: no company abbr found for company '{ext_pr.company}' on {remote_id}.",
                    "ExternalPurchaseReceipt Sync - missing company abbr"
                )

            target_name = (
                f"{company_abbr}-{remote_id}"
                if company_abbr and not remote_id.startswith(f"{company_abbr}-")
                else remote_id
            )

            remote_id_field = None
            pr_meta = frappe.get_meta("Purchase Receipt")
            for candidate in ("custom_remote_id", "remote_id"):
                if pr_meta.has_field(candidate):
                    remote_id_field = candidate
                    break

            if not remote_id_field:
                frappe.throw("Purchase Receipt has neither 'custom_remote_id' nor 'remote_id' field - cannot track sync identity.")

            existing_pr = frappe.db.get_value("Purchase Receipt", {remote_id_field: remote_id}, ["name", "docstatus"])

            if existing_pr:
                existing_name, existing_docstatus = existing_pr

                if source_docstatus != existing_docstatus:
                    try:
                        if source_docstatus == 2:
                            # ORM cancel — reverses SLE + GL Entries properly.
                            # Raw SQL cancel (_set_docstatus_directly) is intentionally disabled.
                            doc = frappe.get_doc("Purchase Receipt", existing_name)
                            _apply_sync_flags(doc)
                            if existing_docstatus == 0:
                                # Draft → must submit first (cancel requires docstatus=1)
                                doc.submit()
                                doc = frappe.get_doc("Purchase Receipt", existing_name)
                            _safe_cancel_doc(doc, log_prefix=f"Purchase Receipt {existing_name}")
                        elif source_docstatus == 1 and existing_docstatus == 0:
                            doc = frappe.get_doc("Purchase Receipt", existing_name)
                            _apply_sync_flags(doc)
                            doc.submit()
                        elif source_docstatus == 0 and existing_docstatus == 1:
                            frappe.log_error(
                                title=f"Cannot unsubmit Purchase Receipt: {existing_name}",
                                message=f"Source docstatus is 0 (Draft) but local is 1 (Submitted). Cannot unsubmit."
                            )
                    except Exception as status_error:
                        if "NegativeStockError" in frappe.get_traceback():
                            pending_submit_retry.append(existing_name)
                        frappe.log_error(
                            title=f"Purchase Receipt Status Update Error: {existing_name}",
                            message=f"Failed to update docstatus: {frappe.get_traceback()}"
                        )

                frappe.db.set_value("External Purchase Receipt", name, "is_synced", 1, update_modified=False)
                results.append({"name": existing_name, "status": "exists"})
                continue

            # -----------------------------------------------------
            # NEW PURCHASE RECEIPT
            # -----------------------------------------------------
            pr = frappe.new_doc("Purchase Receipt")
            pr.set(remote_id_field, remote_id)
            pr.name = target_name
            pr.flags.name_set = True
            _apply_sync_flags(pr)

            set_source_site_fields(pr, site_url=getattr(ext_pr, "source_site", None))

            pr_table_fields = _get_table_fieldnames(pr_meta)
            for field, value in ext_pr.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in pr_table_fields
                    and field not in ["remote_id", "custom_remote_id", "status", "return_against", "is_synced"]
                    and hasattr(pr, field)
                ):
                    pr.set(field, value)

            # ------------------------------------------------
            # RESOLVE RETURN AGAINST (Header)
            # ------------------------------------------------
            if ext_pr.get("return_against"):
                local_return_against = resolve_local_pr_for_item(ext_pr.return_against, company_abbr)
                if local_return_against:
                    pr.return_against = local_return_against
                else:
                    frappe.log_error(
                        title=f"PR Sync - return_against '{ext_pr.return_against}' unresolved",
                        message="Return against PR could not be resolved. Leaving blank."
                    )
            else:
                pr.return_against = None

            # -----------------------------
            # ITEMS (Mirroring SI Logic Exactly)
            # -----------------------------
            pr.set("items", [])
            pr_item_meta = frappe.get_meta("Purchase Receipt Item")
            has_custom_remote_id = pr_item_meta.has_field("custom_remote_id")

            for row in ext_pr.items:
                # 1. Copy fields exactly like Sales Invoice does
                item_row = {}
                for field, value in row.as_dict().items():
                    if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                        item_row[field] = value

                # 2. Validate item exists locally
                item_code = item_row.get("item_code")
                if not item_code or not frappe.db.exists("Item", item_code):
                    frappe.log_error(
                        title=f"PR Sync: Item not found or empty - {item_code}",
                        message=f"Skipping item for External PR '{remote_id}'."
                    )
                    continue

                # ⚠️ BRUTE FORCE SAFETY NET 1: Remove any remote leak immediately
                item_row.pop("purchase_receipt_item", None)
                item_row.pop("purchase_receipt_detail", None)
                item_row.pop("purchase_receipt", None)

                # 3. Resolve PO
                if row.get("purchase_order"):
                    local_po = resolve_local_po(row.purchase_order, company_abbr)
                    item_row["purchase_order"] = local_po
                    item_row["purchase_order_item"] = get_po_detail(
                        local_po, row.item_code, row.get("purchase_order_item")
                    ) if local_po else None
                    sync_item_with_po_reference(item_row)
                else:
                    item_row["purchase_order"] = None
                    item_row["purchase_order_item"] = None

                # 4. Resolve MR
                if row.get("material_request"):
                    local_mr = resolve_local_mr(row.material_request, company_abbr)
                    item_row["material_request"] = local_mr
                    item_row["material_request_item"] = get_mr_detail(
                        local_mr, row.item_code
                    ) if local_mr else None
                else:
                    item_row["material_request"] = None
                    item_row["material_request_item"] = None

                # 5. Resolve PI
                if row.get("purchase_invoice"):
                    local_pi = resolve_local_pi(row.purchase_invoice, company_abbr)
                    item_row["purchase_invoice"] = local_pi
                    item_row["purchase_invoice_item"] = get_pi_detail(
                        local_pi, row.item_code, row.get("purchase_invoice_item")
                    ) if local_pi else None
                else:
                    item_row["purchase_invoice"] = None
                    item_row["purchase_invoice_item"] = None

                # 6. Resolve purchase_receipt_item using the original source row name
                if pr.return_against:
                    remote_pr_item = (
                        row.get("purchase_receipt_item")
                        or row.get("purchase_receipt_detail")
                        or row.get("name")
                    )
                    local_pr_item = get_pr_detail_for_item(
                        pr.return_against,
                        row.item_code,
                        remote_pr_item,
                    )
                    item_row["purchase_receipt_item"] = local_pr_item
                    item_row["purchase_receipt"] = pr.return_against
                else:
                    item_row["purchase_receipt_item"] = None
                    item_row["purchase_receipt"] = None

                # 7. Map external row identity
                if has_custom_remote_id:
                    item_row["custom_remote_id"] = row.name

                # 8. Resolve warehouse
                if not item_row.get("warehouse"):
                    item_row["warehouse"] = _resolve_default_warehouse(ext_pr.company)

                # Append ONLY ONCE
                pr.append("items", item_row)

            # If ALL items were skipped, don't create an empty PR
            if not pr.items:
                frappe.log_error(
                    title=f"PR Sync: No valid items for {remote_id}",
                    message=f"All items skipped because they don't exist in local Item Master."
                )
                frappe.db.set_value("External Purchase Receipt", name, "is_synced", 1, update_modified=False)
                results.append({"name": remote_id, "status": "skipped", "error": "No valid items"})
                continue

            # -----------------------------
            # TAXES & OTHER CHILD TABLES
            # -----------------------------
            _append_matching_child_table(pr, ext_pr, "taxes")
            for table_fieldname in ("pricing_rules", "supplied_items"):
                _append_matching_child_table(pr, ext_pr, table_fieldname)

            # -----------------------------
            # INSERT
            # -----------------------------
            _validate_or_clear_return_link(pr, ext_pr)
            _ensure_no_remote_ids_in_return_refs(pr)
            pr.set_posting_time = 1
            pr.insert(ignore_permissions=True)
            clean_duplicate_pr_items(pr.name)

            # -----------------------------
            # NAMING SAFETY NET
            # -----------------------------
            final_name = pr.name
            if pr.name != target_name:
                if not frappe.db.exists("Purchase Receipt", target_name):
                    old_name = pr.name
                    frappe.db.sql("""UPDATE `tabPurchase Receipt` SET name = %s WHERE name = %s""", (target_name, old_name))
                    for df in pr_meta.get_table_fields():
                        child_table = df.options
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""UPDATE `tab{0}` SET parent = %s WHERE parent = %s""".format(child_table), (target_name, old_name))
                    final_name = target_name
                else:
                    frappe.log_error(
                        f"Purchase Receipt Sync: could not rename {pr.name} to {target_name} because that name already exists.",
                        "ExternalPurchaseReceipt Sync - name collision"
                    )

            # -----------------------------
            # DOCSTATUS SYNC
            # -----------------------------
            if source_docstatus == 2:
                # ORM cancel — reverses SLE + GL Entries properly.
                # Raw SQL cancel (_set_docstatus_directly) is intentionally disabled.
                # A newly inserted PR is always Draft (docstatus=0), so we must
                # submit first before cancel can proceed via the ORM.
                try:
                    pr_to_cancel = frappe.get_doc("Purchase Receipt", final_name)
                    _apply_sync_flags(pr_to_cancel)
                    pr_to_cancel.submit()
                    pr_to_cancel = frappe.get_doc("Purchase Receipt", final_name)
                    _safe_cancel_doc(pr_to_cancel, log_prefix=f"Purchase Receipt {final_name}")
                except Exception as cancel_err:
                    if "NegativeStockError" in frappe.get_traceback():
                        pending_submit_retry.append(final_name)
                    frappe.log_error(
                        title=f"Purchase Receipt Cancel Error: {final_name}",
                        message=f"ORM submit/cancel failed: {frappe.get_traceback()}"
                    )
            elif source_docstatus == 1:
                try:
                    pr = frappe.get_doc("Purchase Receipt", final_name)
                    _apply_sync_flags(pr)
                    pr.submit()
                except Exception as submit_error:
                    if "NegativeStockError" in frappe.get_traceback():
                        pending_submit_retry.append(final_name)
                    frappe.log_error(
                        title=f"Purchase Receipt Submit Error: {final_name}",
                        message=f"Source docstatus was 1 (Submitted) but submit failed: {frappe.get_traceback()}"
                    )

            frappe.db.set_value("External Purchase Receipt", name, "is_synced", 1, update_modified=False)
            results.append({"name": final_name, "status": "synced"})

            frappe.db.set_value(
                "External Purchase Receipt",
                name,
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

        except Exception as e:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(
                title=f"Purchase Receipt Sync Error: {name}",
                message=frappe.get_traceback()
            )
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })
            break
        finally:
            release_sync_lock(_lock_name)

    # =========================================================================
    # RETRY PASS FOR SUBMITS THAT FAILED ON NEGATIVE STOCK
    # =========================================================================
    for pr_name in pending_submit_retry:
        try:
            current_docstatus = frappe.db.get_value("Purchase Receipt", pr_name, "docstatus")
            if current_docstatus != 0:
                continue 

            doc = frappe.get_doc("Purchase Receipt", pr_name)
            _apply_sync_flags(doc)
            doc.submit()

            for r in results:
                if r.get("name") == pr_name:
                    r["status"] = "synced (retry)"
                    break
        except Exception:
            frappe.log_error(
                title=f"Purchase Receipt Submit Retry Failed: {pr_name}",
                message=frappe.get_traceback()
            )
            break

    return results
