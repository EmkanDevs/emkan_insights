import frappe
import json
from frappe.utils import flt, cint

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
# SAFE LOG ERROR (fixes 140-char title truncation)
# -----------------------------------------------------

def _log(title, message):
    """Log error with guaranteed short title."""
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


# -----------------------------------------------------
# GL ENTRY REVERSAL HELPER
# -----------------------------------------------------

def _cancel_gl_entries_for_voucher(doctype, docname):
    """
    Properly reverse GL Entries for a voucher by creating cancellation entries.

    ERPNext normally does this inside on_cancel via make_gl_entries(cancel=True).
    When we force-cancel via raw SQL that hook is skipped, so we call this
    function explicitly to ensure the cancellation GL entries are written.

    Strategy:
      1. Try ERPNext's make_reverse_gl_entries (available in v13/v14/v15).
      2. If that import fails, mark existing GL entries as is_cancelled=1 as a
         last-resort — this at least prevents double-counting even though it
         doesn't create the reversal rows.
    """
    existing_gl_entries = frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": doctype,
            "voucher_no": docname,
            "is_cancelled": 0,
        },
        fields=["*"],
    )

    if not existing_gl_entries:
        # Nothing to reverse — either already cancelled or never submitted.
        return

    try:
        from erpnext.accounts.general_ledger import make_reverse_gl_entries
        make_reverse_gl_entries(existing_gl_entries)
        _log(
            f"GL entries reversed for {doctype} {docname}",
            f"Reversed {len(existing_gl_entries)} GL entry row(s).",
        )
    except ImportError:
        # make_reverse_gl_entries not available on this ERPNext version.
        # Fall back: mark originals as cancelled so reports exclude them.
        frappe.db.sql(
            """
            UPDATE `tabGL Entry`
            SET is_cancelled = 1, modified = NOW()
            WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
            """,
            (doctype, docname),
        )
        _log(
            f"GL entries marked cancelled (fallback) for {doctype} {docname}",
            f"make_reverse_gl_entries not available; {len(existing_gl_entries)} row(s) marked is_cancelled=1.",
        )
    except Exception as gle_err:
        _log(
            f"GL entry reversal failed for {doctype} {docname}",
            f"Error: {str(gle_err)}\n\n{frappe.get_traceback()}",
        )


# -----------------------------------------------------
# FORCE CANCEL HELPERS
# -----------------------------------------------------

def _force_cancel_doc(doctype, docname):
    """
    Force cancel a document by directly updating the database.
    Use as fallback when normal cancel() fails due to linked docs or validations.
    No explicit commit — the ORM caller or request end handles persistence.

    IMPORTANT: We reverse GL Entries BEFORE setting docstatus=2 so that the
    cancellation rows are written with the correct voucher reference while the
    document is still technically submitted (docstatus=1).  Without this step
    the on_cancel hook is never called and the reversal GL entries are never
    created, causing the local GL entry count to diverge from the remote.
    """
    # Step 1: Reverse GL Entries first (while docstatus is still 1 / submitted)
    _cancel_gl_entries_for_voucher(doctype, docname)

    # Step 2: Set docstatus=2 on the parent document via SQL
    frappe.db.sql("""
        UPDATE `tab{doctype}`
        SET docstatus = 2, 
            status = 'Cancelled', 
            modified = NOW(), 
            modified_by = SUBSTRING_INDEX(USER(), '@', 1)
        WHERE name = %s
    """.format(doctype=doctype), docname)

    # Step 3: Cascade docstatus=2 to all child tables
    meta = frappe.get_meta(doctype)
    for df in meta.get_table_fields():
        child_doctype = df.options
        if frappe.db.exists("DocType", child_doctype):
            frappe.db.sql("""
                UPDATE `tab{child_doctype}`
                SET docstatus = 2, 
                    modified = NOW(), 
                    modified_by = SUBSTRING_INDEX(USER(), '@', 1)
                WHERE parent = %s AND parenttype = %s
            """.format(child_doctype=child_doctype), (docname, doctype))

    frappe.clear_document_cache(doctype, docname)


def _safe_cancel_doc(doc, log_prefix=""):
    """
    Safely cancel a document with fallback to force cancel.
    Uses doc.cancel() (ORM) first, then raw SQL fallback.
    Returns True if successful, raises exception if both methods fail.
    """
    try:
        doc.flags.ignore_permissions = True
        doc.cancel()  # ORM submit — writes to DB, no explicit commit needed
        _log(f"{log_prefix} cancelled normally", "")
        return True
    except Exception as normal_cancel_err:
        _log(f"{log_prefix} normal cancel failed, attempting force cancel",
             f"Error: {str(normal_cancel_err)}")
        
        try:
            _force_cancel_doc(doc.doctype, doc.name)
            _log(f"{log_prefix} force cancelled successfully", "")
            return True
        except Exception as force_cancel_err:
            _log(f"{log_prefix} force cancel also failed",
                 f"Normal: {str(normal_cancel_err)}\nForce: {str(force_cancel_err)}")
            raise


# -----------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------

def _build_company_prefixed_name(company_abbr, document_name):
    document_name = (document_name or "").strip()

    if not company_abbr or not document_name:
        return document_name

    if document_name.startswith(f"{company_abbr}-"):
        return document_name

    return f"{company_abbr}-{document_name}"


def resolve_local_po(remote_or_local_po):
    if not remote_or_local_po:
        return None

    local_name = frappe.db.get_value(
        "Purchase Order", {"remote_id": remote_or_local_po}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return remote_or_local_po

    company_abbr = frappe.db.get_default("company_abbr") or ""
    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return prefixed

    return None


def resolve_local_pr(remote_or_local_pr, company_abbr=None):
    if not remote_or_local_pr:
        return None

    local_name = frappe.db.get_value(
        "Purchase Receipt", {"remote_id": remote_or_local_pr}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Receipt", remote_or_local_pr):
        return remote_or_local_pr

    if company_abbr and not remote_or_local_pr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pr}"
        if frappe.db.exists("Purchase Receipt", prefixed):
            return prefixed

    return None


def resolve_local_mr(remote_or_local_mr):
    if not remote_or_local_mr:
        return None

    local_name = frappe.db.get_value(
        "Material Request", {"remote_id": remote_or_local_mr}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Material Request", remote_or_local_mr):
        return remote_or_local_mr

    company_abbr = frappe.db.get_default("company_abbr") or ""
    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed

    return None

def resolve_local_pinv(remote_or_local_pinv, company_abbr=None):
    if not remote_or_local_pinv:
        return None

    local_name = frappe.db.get_value(
        "Purchase Invoice", {"remote_id": remote_or_local_pinv}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Invoice", remote_or_local_pinv):
        return remote_or_local_pinv

    if company_abbr and not remote_or_local_pinv.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pinv}"
        if frappe.db.exists("Purchase Invoice", prefixed):
            return prefixed

    return None

def get_po_detail(purchase_order, item_code):
    if not purchase_order:
        return None
    return frappe.db.get_value(
        "Purchase Order Item",
        {"parent": purchase_order, "item_code": item_code},
        "name"
    )


def get_pr_detail(purchase_receipt, item_code, remote_pr_item=None):
    """
    Resolve the local Purchase Receipt Item row this invoice item should
    bill against.

    Returns (pr_detail_name, ambiguous) where `ambiguous` is True when we
    deliberately declined to pick a match.

    IMPORTANT: if the remote didn't supply a purchase_receipt_item id (or it
    doesn't match anything locally), and there is more than one PR Item row
    with the same item_code on this receipt, we do NOT guess by falling back
    to "first match by item_code". Guessing here has previously caused one
    PR Item row to get billed by multiple invoices while a sibling row with
    the same item_code goes unbilled — which surfaces later as "Over Billing
    Allowance exceeded" on submit. Leaving the link unresolved is safer than
    picking a possibly-wrong row; the item still gets inserted, just without
    a PR reference, and the caller logs it for manual review.
    """
    if not purchase_receipt:
        return None, False

    if remote_pr_item:
        exact = frappe.db.get_value(
            "Purchase Receipt Item",
            {"parent": purchase_receipt, "custom_remote_id": remote_pr_item},
            "name"
        )
        if exact:
            return exact, False

    candidates = frappe.get_all(
        "Purchase Receipt Item",
        filters={"parent": purchase_receipt, "item_code": item_code},
        pluck="name",
    )

    if len(candidates) == 1:
        return candidates[0], False
    if len(candidates) == 0:
        return None, False

    # Multiple PR Item rows share this item_code and we have no reliable
    # remote_pr_item to disambiguate — refuse to guess.
    return None, True


def get_mr_detail(material_request, item_code):
    if not material_request:
        return None
    return frappe.db.get_value(
        "Material Request Item",
        {"parent": material_request, "item_code": item_code},
        "name"
    )


# -----------------------------------------------------
# DUPLICATE CLEANER
# -----------------------------------------------------

def _dedupe_purchase_invoice_children(pi_name):
    """
    Remove duplicate child rows from Purchase Invoice.
    No explicit commit — relies on transaction management.
    IMPORTANT: this must never raise — a dedupe failure should not make
    an otherwise-successful sync record look like it failed (see caller).
    """

    def _dedupe_table(doctype, parentfield, key_fields, float_fields=()):
        if not frappe.db.exists("DocType", doctype):
            return

        rows = frappe.db.get_all(
            doctype,
            filters={
                "parent": pi_name,
                "parenttype": "Purchase Invoice",
                "parentfield": parentfield,
            },
            fields=["name"] + key_fields + list(float_fields),
            order_by="creation asc",
        )

        seen = set()
        delete_rows = []

        for row in rows:
            key = []

            for field in key_fields:
                key.append(str(row.get(field) or "").strip().lower())

            for field in float_fields:
                key.append(round(flt(row.get(field)), 3))

            key = tuple(key)

            if key in seen:
                delete_rows.append(row.name)
            else:
                seen.add(key)

        if delete_rows:
            frappe.db.delete(
                doctype,
                {"name": ["in", delete_rows]}
            )

    _dedupe_table(
        "Purchase Invoice Item", "items",
        ["item_code", "purchase_order", "purchase_receipt",
         "material_request", "warehouse", "uom"],
        ["qty", "rate"],
    )

    _dedupe_table(
        "Purchase Taxes and Charges", "taxes",
        ["charge_type", "account_head", "description"],
        ["rate", "tax_amount"],
    )

    _dedupe_table(
        "Payment Schedule", "payment_schedule",
        ["payment_term", "due_date"],
        ["payment_amount", "invoice_portion"],
    )


def _safe_dedupe(pi_name):
    """
    Wrapper so a bug inside dedupe (or any future one) can never bubble up
    and get caught by the outer per-record try/except, which would mark an
    already-inserted invoice as "failed" while leaving duplicate rows in place.
    """
    try:
        _dedupe_purchase_invoice_children(pi_name)
    except Exception:
        _log(
            title=f"PI dedupe failed for {pi_name}",
            message=frappe.get_traceback()
        )


def _delete_purchase_invoice_child_rows(pi_name):
    meta = frappe.get_meta("Purchase Invoice")

    for df in meta.get_table_fields():
        if frappe.db.exists("DocType", df.options):
            frappe.db.delete(
                df.options,
                {
                    "parent": pi_name,
                    "parenttype": "Purchase Invoice",
                    "parentfield": df.fieldname,
                },
            )


def _get_child_count(doctype, parent, parenttype):
    return frappe.db.count(doctype, {"parent": parent, "parenttype": parenttype})


def _has_item_count_mismatch(ext_pi, pi_name):
    external_items = _get_child_count("Purchase Invoice Item", ext_pi.name, ext_pi.doctype)
    local_items = _get_child_count("Purchase Invoice Item", pi_name, "Purchase Invoice")

    return external_items != local_items, external_items, local_items


def _get_table_fieldnames(meta):
    return {df.fieldname for df in meta.get_table_fields()}


def _copy_header_fields(source_doc, target_doc):
    """
    Copy scalar fields only. Child tables must be rebuilt deliberately so
    remote child row names cannot collide with local Purchase Invoice rows.

    NOTE: For return invoices, values like total_qty, total, net_total, etc.
    will be negative. These MUST be preserved as-is — they are used as a
    starting point only and _recalculate_totals() will recompute them.
    """
    target_table_fields = _get_table_fieldnames(target_doc.meta)

    for field, value in source_doc.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field == "remote_id":
            continue
        if field in target_table_fields:
            continue
        if hasattr(target_doc, field):
            # Preserve exact value including negative for returns
            target_doc.set(field, value)


def _copy_child_row_fields(row, target_child_doctype):
    target_meta = frappe.get_meta(target_child_doctype)
    target_table_fields = _get_table_fieldnames(target_meta)
    row_data = {"doctype": target_child_doctype}

    for field, value in row.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field in target_table_fields:
            continue
        if target_meta.has_field(field):
            row_data[field] = value

    return row_data


# -----------------------------------------------------
# TOTALS RECALCULATION (root fix for debit/credit mismatch)
# -----------------------------------------------------

def _recalculate_totals(pi):
    """
    Recompute item amounts, tax totals, and grand total from what is
    ACTUALLY in the doc's child tables right now — rather than trusting the
    scalar total fields that were copied verbatim from the remote doc by
    _copy_header_fields.
    Must be called AFTER items/taxes are fully inserted and the doc has been
    reloaded, and BEFORE every pi.submit() call.
    """
    try:
        pi.calculate_taxes_and_totals()
    except Exception:
        _log(
            title=f"PI {pi.name} totals recalculation failed",
            message=frappe.get_traceback()
        )


# -----------------------------------------------------
# INSERT ITEMS ONE-BY-ONE WITH ERROR ISOLATION
# -----------------------------------------------------

def _insert_pi_items_safely(pi_name, ext_pi, company_abbr=None, is_return=None):
    ...
    success_count = 0
    errors = []
    if is_return is None:
        is_return = cint(ext_pi.get("is_return", 0))
    else:
        is_return = cint(is_return)

    for idx, row in enumerate(ext_pi.items, start=1):
        try:
            # Get raw qty and convert - preserve the sign!
            raw_qty = row.get("qty")
            qty = flt(raw_qty)
            
            # ---------------------------------------------------------
            # Quantity validation - mirrors ERPNext's validate_qty():
            #   "if not self.is_return and flt(item.qty) <= 0"
            #
            # RETURN invoices (is_return=1):
            #   - Negative qty: VALID (expected for returns/debit notes)
            #   - Zero qty: INVALID (skip)
            #   - Positive qty: Allow (some systems use positive with is_return flag)
            #
            # REGULAR invoices (is_return=0):
            #   - Positive qty: VALID
            #   - Zero qty: INVALID (skip)
            #   - Negative qty: INVALID (skip - would fail at submit time)
            # ---------------------------------------------------------
            if qty == 0:
                error_msg = (
                    f"Item {idx} (row name: {row.name}, item_code: {row.item_code}): "
                    f"qty is zero (remote qty={raw_qty}); skipping insert."
                )
                errors.append(error_msg)
                _log(
                    title=f"PI item skipped (zero qty) for {pi_name}",
                    message=error_msg
                )
                continue
            
            if not is_return and qty < 0:
                # Regular (non-return) invoices cannot have negative qty
                # This would fail at submit time with "quantity must be positive"
                error_msg = (
                    f"Item {idx} (row name: {row.name}, item_code: {row.item_code}): "
                    f"negative qty ({qty}) not allowed for non-return invoice; skipping."
                )
                errors.append(error_msg)
                _log(
                    title=f"PI item skipped (negative qty on non-return) for {pi_name}",
                    message=error_msg
                )
                continue

            # Build item row - start fresh to avoid any field contamination
            item_row = {"doctype": "Purchase Invoice Item"}

            for field, value in row.as_dict().items():
                if field in SYSTEM_FIELDS:
                    continue
                if field in IGNORE_ITEM_FIELDS:
                    continue
                if field in {"purchase_order", "purchase_receipt",
                             "po_detail", "pr_detail", "material_request",
                             "material_request_item"}:
                    continue
                # IMPORTANT: Copy value AS-IS to preserve negative qty for returns
                # Do NOT use abs() or any transformation here
                item_row[field] = value

            # Resolve PO
            if row.purchase_order:
                local_po = resolve_local_po(row.purchase_order)
                item_row["purchase_order"] = local_po
                item_row["po_detail"] = get_po_detail(local_po, row.item_code) if local_po else None
            else:
                item_row["purchase_order"] = None
                item_row["po_detail"] = None

            # Resolve PR
            if row.purchase_receipt:
                local_pr = resolve_local_pr(row.purchase_receipt, company_abbr)
                if local_pr:
                    pr_detail, ambiguous = get_pr_detail(
                        local_pr, row.item_code,
                        remote_pr_item=row.get("purchase_receipt_item")
                    )
                    if ambiguous:
                        item_row["purchase_receipt"] = None
                        item_row["pr_detail"] = None
                        warn_msg = (
                            f"Item {idx} (row name: {row.name}, item_code: {row.item_code}): "
                            f"multiple Purchase Receipt Item rows on {local_pr} share this "
                            f"item_code and no purchase_receipt_item id was provided to "
                            f"disambiguate; left unlinked to avoid mis-billing. Needs manual review."
                        )
                        errors.append(warn_msg)
                        _log(
                            title=f"PI item PR link ambiguous for {pi_name}",
                            message=warn_msg
                        )
                    else:
                        item_row["purchase_receipt"] = local_pr
                        item_row["pr_detail"] = pr_detail
                else:
                    item_row["purchase_receipt"] = None
                    item_row["pr_detail"] = None
            else:
                item_row["purchase_receipt"] = None
                item_row["pr_detail"] = None

            # Resolve MR
            if row.get("material_request"):
                local_mr = resolve_local_mr(row.material_request)
                item_row["material_request"] = local_mr
                item_row["material_request_item"] = get_mr_detail(local_mr, row.item_code) if local_mr else None
            else:
                item_row["material_request"] = None
                item_row["material_request_item"] = None

            if is_return:
                current_qty = flt(item_row.get("qty", 0))
                if current_qty > 0:
                    item_row["qty"] = -current_qty
                    frappe.logger().debug(
                        f"PI {pi_name} item {idx}: converted qty {current_qty} -> {-current_qty} for return"
                    )

            # Parent linkage
            item_row["parent"] = pi_name
            item_row["parentfield"] = "items"
            item_row["parenttype"] = "Purchase Invoice"
            item_row["idx"] = idx
            item_row["name"] = frappe.generate_hash(length=10)

            # Use db_insert() directly to bypass ALL validation
            # This ensures negative qty values for returns are inserted exactly as-is
            child_doc = frappe.get_doc(item_row)
            child_doc.db_insert()

            # Log successful insert for return items with negative qty (debugging)
            if is_return and qty < 0:
                frappe.logger().debug(
                    f"PI {pi_name} item {idx}: inserted with negative qty={qty} for return"
                )

            success_count += 1

        except Exception as e:
            error_msg = f"Item {idx} (row name: {row.name}, item_code: {row.item_code}): {str(e)}"
            errors.append(error_msg)
            _log(
                title=f"PI item insert failed for {pi_name}",
                message=error_msg
            )

    return success_count, errors


# -----------------------------------------------------
# HANDLE EXISTING DOC DOCSTATUS SYNC
# -----------------------------------------------------

def _sync_existing_docstatus(existing_name, ext_pi):
    """
    Handle docstatus sync for already-existing Purchase Invoice documents.
    Uses doc.submit() / doc.cancel() (ORM) — no explicit commits.
    This is the KEY FIX — previously existing docs were just skipped.
    """
    local_docstatus = frappe.db.get_value("Purchase Invoice", existing_name, "docstatus")
    target_docstatus = cint(ext_pi.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists"}

    if target_docstatus in (1, 2):
        local_item_count = _get_child_count("Purchase Invoice Item", existing_name, "Purchase Invoice")
        if local_item_count == 0:
            _log(
                title=f"PI {existing_name} has zero items, submit skipped",
                message="Cannot submit/cancel a Purchase Invoice with zero items.",
            )
            return {
                "name": existing_name,
                "status": "exists",
                "warning": "zero items; submit skipped",
            }

    try:
        pi = frappe.get_doc("Purchase Invoice", existing_name)
        pi.flags.ignore_permissions = True

        if target_docstatus == 1:
            if local_docstatus == 0:
                _recalculate_totals(pi)
                pi.submit() 
                _safe_dedupe(pi.name)
                _log(f"PI {pi.name} docstatus synced: Draft -> Submitted", "")
                return {"name": pi.name, "status": "docstatus_synced", "docstatus": 1}
            else:
                _log(
                    f"PI {existing_name} cannot transition from Cancelled to Submitted",
                    f"local={local_docstatus}, external={target_docstatus}"
                )
                return {"name": existing_name, "status": "exists", "warning": "cannot un-cancel"}

        elif target_docstatus == 2:
            if local_docstatus == 0:
                # Draft -> need to submit first, then cancel
                _recalculate_totals(pi)
                pi.submit()  
                pi = frappe.get_doc("Purchase Invoice", existing_name)
            
            # Now cancel (whether it was 0 or 1 before)
            _safe_cancel_doc(pi, log_prefix=f"PI {existing_name}")
            _safe_dedupe(pi.name)
            
            final_docstatus = frappe.db.get_value("Purchase Invoice", pi.name, "docstatus")
            if final_docstatus == 2:
                _log(f"PI {pi.name} docstatus synced: -> Cancelled", "")
                return {"name": pi.name, "status": "docstatus_synced", "docstatus": 2}
            else:
                _log(f"PI {pi.name} cancel verification failed",
                     f"Expected docstatus=2, got {final_docstatus}")
                return {"name": pi.name, "status": "exists", "warning": "cancel verification failed"}

        else:
            _log(
                f"PI {existing_name} cannot transition to Draft",
                f"local={local_docstatus}, external={target_docstatus}"
            )
            return {"name": existing_name, "status": "exists", "warning": "cannot revert to draft"}

    except Exception as trans_err:
        _log(f"PI {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(trans_err)}


# -----------------------------------------------------
# SINGLE DOCUMENT SYNC
# -----------------------------------------------------

def _sync_single_purchase_invoice(source_doctype, name):
    """
    Process a single Purchase Invoice sync.
    All database writes (insert/save/submit/cancel/db_set/sql) happen within
    the current transaction. Frappe commits automatically at request end.
    """
    ext_pi = frappe.get_doc(source_doctype, name)
    remote_id = ext_pi.name

    # Defensive reload if items empty
    if not ext_pi.items:
        frappe.clear_document_cache(source_doctype, name)
        ext_pi.reload()

        if not ext_pi.items:
            # FIX: Get the actual child doctype from meta instead of
            # guessing f"{source_doctype} Item" which produces a
            # non-existent table name (e.g. "External Purchase Invoice Item").
            actual_count = 0
            for df in ext_pi.meta.get_table_fields():
                if df.fieldname == "items":
                    actual_count = frappe.db.count(df.options, {"parent": name})
                    break

            if actual_count > 0:
                ext_pi = frappe.get_doc(source_doctype, name)
                for field in ext_pi.meta.get_table_fields():
                    ext_pi.load_children(field.fieldname)

            if not ext_pi.items:
                _log(
                    title="PI Sync - external doc has 0 items",
                    message=f"External PI {name} has 0 items even after reload. "
                            f"Proceeding with empty items table."
                )

    # Build target name
    company_abbr = frappe.db.get_value('Company', ext_pi.company, 'abbr') or ''

    if not company_abbr:
        _log(
            title="PI Sync - missing company abbr",
            message=f"No company abbr for '{ext_pi.company}' on {remote_id}."
        )

    target_name = _build_company_prefixed_name(company_abbr, remote_id)

    # Check already synced
    existing = frappe.db.get_value(
        "Purchase Invoice", {"remote_id": remote_id}, "name"
    )

    if not existing:
        existing = frappe.db.exists("Purchase Invoice", target_name)

    # ============================================================
    # Sync docstatus for existing documents instead of skip
    # ============================================================
    if existing:
        local_docstatus = cint(frappe.db.get_value("Purchase Invoice", existing, "docstatus"))
        target_docstatus = cint(ext_pi.docstatus)
        has_item_mismatch, expected_items, actual_items = _has_item_count_mismatch(ext_pi, existing)

        if local_docstatus == 0 and (target_docstatus in (1, 2) or has_item_mismatch):
            _log(
                f"PI {existing} rebuilding draft before docstatus sync",
                f"External docstatus is {target_docstatus}; deleting local draft and rebuilding from {remote_id}. "
                f"External items: {expected_items}, local items: {actual_items}."
            )
            _delete_purchase_invoice_child_rows(existing)
            frappe.delete_doc(
                "Purchase Invoice",
                existing,
                force=True,
                ignore_permissions=True,
                ignore_missing=True,
            )
            # Second pass to catch any orphans the ORM might have missed
            _delete_purchase_invoice_child_rows(existing)
        elif has_item_mismatch:
            _log(
                f"PI {existing} item count mismatch",
                f"Cannot rebuild non-draft Purchase Invoice safely. "
                f"External docstatus: {target_docstatus}, local docstatus: {local_docstatus}. "
                f"External items: {expected_items}, local items: {actual_items}."
            )
            return {
                "name": existing,
                "status": "sync_error",
                "docstatus": local_docstatus,
                "expected_docstatus": target_docstatus,
                "items_synced": actual_items,
                "items_expected": expected_items,
                "error": "Item count mismatch on non-draft Purchase Invoice; manual rebuild needed."
            }
        else:
            return _sync_existing_docstatus(existing, ext_pi)

    # Clean up any orphaned child rows for the target name
    _delete_purchase_invoice_child_rows(target_name)

    pi = frappe.new_doc("Purchase Invoice")

    # Set name BEFORE anything else
    pi.remote_id = remote_id
    pi.name = target_name
    pi.flags.name_set = True

    if hasattr(pi, "source_site"):
        pi.source_site = ext_pi.source_site

    # Copy scalar header fields only
    _copy_header_fields(ext_pi, pi)
    pi.set_posting_time = 1

    # Fix currency
    if not pi.currency:
        pi.currency = frappe.get_cached_value(
            "Company", pi.company, "default_currency"
        )

    items_have_negative_qty = any(
        flt(row.get("qty")) < 0 for row in (ext_pi.get("items") or [])
    )
    is_return_effective = cint(ext_pi.get("is_return", 0)) or items_have_negative_qty

    # ---------------------------------------------------------------
    # Defer is_return / return_against until AFTER the parent insert.
    #
    # ERPNext's validate_returned_items() (invoked from validate()
    # during insert()) throws "Atleast one item should be entered with
    # negative quantity in return document" when is_return=1 but the
    # items table is empty. Here we insert the parent first and attach
    # item rows afterwards via db_insert() to bypass validation, so we
    # must NOT set is_return=1 before insert(). We stash the resolved
    # values and apply them via db_set() once the parent row exists.
    # At submit() time validate() runs again — by then the items are
    # attached with negative qty, so validate_returned_items() passes.
    # ---------------------------------------------------------------
    deferred_is_return = 0
    deferred_return_against = None
    if is_return_effective:
        deferred_is_return = 1
        deferred_return_against = resolve_local_pinv(ext_pi.return_against, company_abbr)

    # Clear any is_return/return_against that _copy_header_fields may have
    # copied from the remote doc so the insert() validate hook doesn't trip.
    pi.is_return = 0
    pi.return_against = None

    # ---------------------------------------------------------------
    # Zero out negative total scalar fields before insert.
    #
    # _copy_header_fields() copied the remote return doc's totals verbatim
    # (negative for returns). ERPNext's validate() only skips the
    # "base_grand_total must be >= 0" check when is_return=1, but we just
    # cleared is_return (see above) so the check runs and rejects the
    # negative base_grand_total. These scalar totals are meaningless with
    # an empty items table anyway — _recalculate_totals() recomputes them
    # from the actual child rows before submit(). Zero them here so insert
    # passes; the real values are restored at submit time.
    # ---------------------------------------------------------------
    if deferred_is_return:
        for _total_field in (
            "total", "base_total", "net_total", "base_net_total",
            "grand_total", "base_grand_total",
            "rounded_total", "base_rounded_total",
            "total_taxes_and_charges", "base_total_taxes_and_charges",
            "discount_amount", "base_discount_amount",
            "total_advance", "base_total_advance",
            "outstanding_amount", "base_outstanding_amount",
        ):
            if hasattr(pi, _total_field):
                pi.set(_total_field, 0)

    # ITEMS: placeholder — items inserted separately below
    pi.set("items", [])

    # TAXES
    if ext_pi.get("taxes"):
        pi.set("taxes", [])
        for row in ext_pi.taxes:
            tax_row = _copy_child_row_fields(row, "Purchase Taxes and Charges")
            pi.append("taxes", tax_row)

    # PAYMENT SCHEDULE
    if ext_pi.get("payment_schedule"):
        pi.set("payment_schedule", [])
        for idx, row in enumerate(ext_pi.payment_schedule, start=1):
            pay_row = {
                "doctype": "Payment Schedule",
                "idx": idx,
                "payment_term": row.payment_term,
                "description": row.description,
                "due_date": row.due_date,
                "invoice_portion": row.invoice_portion,
                "discount": row.discount,
                "payment_amount": row.payment_amount,
                "base_payment_amount": row.base_payment_amount,
            }
            pi.append("payment_schedule", pay_row)

    # FLAGS
    # items is mandatory (reqd=1) on Purchase Invoice, but we insert the
    # parent first and attach item rows via db_insert() afterwards. Without
    # ignore_mandatory, insert() throws "Data missing in table: Items".
    pi.flags.ignore_permissions = True
    pi.flags.ignore_naming_series = True

    # INSERT PARENT ONLY (no items yet) — ORM insert writes to DB
    pi.insert(
        ignore_permissions=True,
        ignore_mandatory=True,
    )

    # Ensure remote_id is set via ORM (safety net, no commit needed)
    pi.db_set("remote_id", remote_id, update_modified=False)

    # Apply deferred return flags now that the parent row exists and items
    # will be attached next. Using db_set() bypasses validate() so we don't
    # re-trigger validate_returned_items() before items are in place.
    if deferred_is_return:
        pi.db_set("is_return", 1, update_modified=False)
        pi.db_set("return_against", deferred_return_against, update_modified=False)
        # Keep the in-memory doc in sync for any subsequent ORM calls
        pi.is_return = 1
        pi.return_against = deferred_return_against

    # INSERT ITEMS ONE-BY-ONE WITH ERROR ISOLATION
    success_count, item_errors = _insert_pi_items_safely(
        pi.name, ext_pi, company_abbr, is_return=is_return_effective
    )

    if item_errors:
        _log(
            title=f"PI {pi.name} - {len(item_errors)} item errors",
            message="Item insert errors:\n" + "\n".join(item_errors)
        )

    # VERIFY ITEM COUNT — abort before submit if items are missing.
    # Submitting with fewer items than the remote would cause make_gl_entries()
    # to create fewer GL lines than exist on the remote, producing the
    # GL entry count mismatch that was reported.
    actual_item_count = _get_child_count(
        "Purchase Invoice Item", pi.name, "Purchase Invoice"
    )
    expected_item_count = len(ext_pi.get("items") or [])

    # ADD THIS BLOCK — a header with zero items can never submit (items is
    # mandatory), so don't even try; leave it as Draft and flag it instead
    # of letting pi.submit() blow up with "Data missing in table: Items".
    if expected_item_count == 0:
        _log(
            title=f"PI Sync - {pi.name} has zero items, submit skipped",
            message=(
                f"External doc {remote_id} has 0 items in its items table "
                f"even after the reload/fallback checks earlier in this "
                f"function. Leaving local copy as Draft. This likely means "
                f"either the remote document genuinely has no items, or its "
                f"child rows failed to load — worth checking {remote_id} "
                f"directly on the remote site."
            ),
        )
        return {
            "name": pi.name,
            "status": "sync_error",
            "docstatus": 0,
            "expected_docstatus": cint(ext_pi.docstatus),
            "items_synced": 0,
            "items_expected": 0,
            "error": "External document has zero items; submit skipped to avoid 'Data missing in table: Items'.",
        }

    if actual_item_count != expected_item_count:
        _log(
            title=f"PI Sync - items mismatch for {pi.name}",
            message=f"Expected {expected_item_count} items, "
                    f"found {actual_item_count} after insert. "
                    f"Successfully inserted: {success_count}, "
                    f"Errors: {len(item_errors)}. "
                    f"Aborting submit to prevent GL entry count divergence."
        )
        # Return a sync_error so the caller knows this PI needs attention.
        # Do NOT submit — submitting with partial items creates the wrong
        # number of GL entries (root cause of the 2875 vs 2847 mismatch).
        return {
            "name": pi.name,
            "status": "sync_error",
            "docstatus": 0,
            "expected_docstatus": cint(ext_pi.docstatus),
            "items_synced": actual_item_count,
            "items_expected": expected_item_count,
            "error": f"Item count mismatch ({actual_item_count}/{expected_item_count}); "
                     f"submit aborted to prevent GL entry divergence. "
                     f"Item errors: {'; '.join(item_errors) if item_errors else 'none'}.",
        }

    # Clean duplicates
    _safe_dedupe(pi.name)

    # -------------------------------------------------
    # NAMING SAFETY NET
    # -------------------------------------------------
    if pi.name != target_name:
        if not frappe.db.exists("Purchase Invoice", target_name):
            old_name = pi.name

            frappe.db.sql("""
                UPDATE `tabPurchase Invoice`
                SET name=%s
                WHERE name=%s
            """, (target_name, old_name))

            for child in [
                "Purchase Invoice Item",
                "Purchase Taxes and Charges",
                "Payment Schedule",
                "Purchase Invoice Advance",
                "Purchase Invoice Deduction",
                "Purchase Invoice Tax",
            ]:
                if frappe.db.exists("DocType", child):
                    frappe.db.sql(f"""
                        UPDATE `tab{child}`
                        SET parent=%s
                        WHERE parent=%s
                    """, (target_name, old_name))

            pi.name = target_name
        else:
            _log(
                title="PI Sync - name collision",
                message=f"Cannot rename {pi.name} to {target_name} - already exists."
            )

    # Reload to pick up the correct name and fresh data
    frappe.clear_document_cache("Purchase Invoice", pi.name)
    pi = frappe.get_doc("Purchase Invoice", pi.name)

    pi.flags.ignore_permissions = True
    pi.flags.ignore_naming_series = True

    # -------------------------------------------------
    # DOCSTATUS SYNC FOR NEW DOCUMENTS
    # -------------------------------------------------
    target_docstatus = cint(ext_pi.docstatus)

    _log(f"PI {pi.name} pre-docstatus-sync",
         f"External docstatus: {target_docstatus}, Current docstatus: {pi.docstatus}")

    if target_docstatus == 1:
        # Submit via ORM
        try:
            _recalculate_totals(pi)
            pi.submit()
            _safe_dedupe(pi.name)
            _log(f"PI {pi.name} submitted successfully", "")
            
            actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
            return {
                "name": pi.name,
                "status": "synced",
                "docstatus": 1,
                "items_synced": actual_item_count,
                "items_expected": expected_item_count
            }

        except Exception as submit_err:
            tb = frappe.get_traceback()
            _log(f"PI {pi.name} SUBMIT FAILED",
                 f"Error: {str(submit_err)}\n\nTraceback:\n{tb}")
            
            actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
            return {
                "name": pi.name,
                "status": "sync_error",
                "docstatus": frappe.db.get_value("Purchase Invoice", pi.name, "docstatus"),
                "expected_docstatus": 1,
                "error": f"Submit failed: {str(submit_err)}",
                "items_synced": actual_item_count,
                "items_expected": expected_item_count
            }

    elif target_docstatus == 2:
        # Submit first via ORM, then cancel
        try:
            _recalculate_totals(pi)
            pi.submit()
            _safe_dedupe(pi.name)
            _log(f"PI {pi.name} submitted (step 1 of cancel)", "")

            # Re-fetch fresh doc and cancel
            pi = frappe.get_doc("Purchase Invoice", pi.name)
            _safe_cancel_doc(pi, log_prefix=f"PI {pi.name}")
            _safe_dedupe(pi.name)
            
            # Verify
            final_docstatus = frappe.db.get_value("Purchase Invoice", pi.name, "docstatus")
            final_status = frappe.db.get_value("Purchase Invoice", pi.name, "status")
            
            _log(f"PI {pi.name} cancel complete",
                 f"Final docstatus: {final_docstatus}, Final status: {final_status}")
            
            actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
            
            if final_docstatus == 2:
                return {
                    "name": pi.name,
                    "status": "synced",
                    "docstatus": 2,
                    "items_synced": actual_item_count,
                    "items_expected": expected_item_count
                }
            else:
                _log(f"PI {pi.name} CANCEL VERIFICATION FAILED",
                     f"Expected docstatus=2, got {final_docstatus}")
                return {
                    "name": pi.name,
                    "status": "sync_error",
                    "error": f"Cancel verification failed - docstatus is {final_docstatus}",
                    "items_synced": actual_item_count,
                    "items_expected": expected_item_count
                }

        except Exception as cancel_err:
            tb = frappe.get_traceback()
            _log(f"PI {pi.name} CANCEL FAILED - CRITICAL",
                 f"Error: {str(cancel_err)}\n\nTraceback:\n{tb}")
            
            # Last resort: Force cancel via SQL
            try:
                _force_cancel_doc("Purchase Invoice", pi.name)
                _log(f"PI {pi.name} force cancelled as last resort", "")
                
                actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                return {
                    "name": pi.name,
                    "status": "synced",
                    "docstatus": 2,
                    "items_synced": actual_item_count,
                    "items_expected": expected_item_count
                }
            except Exception as force_err:
                _log(f"PI {pi.name} FORCE CANCEL ALSO FAILED",
                     f"Force error: {str(force_err)}")
                
                actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                return {
                    "name": pi.name,
                    "status": "synced_submitted",
                    "error": f"Cancel failed: {str(cancel_err)}",
                    "items_synced": actual_item_count,
                    "items_expected": expected_item_count
                }

    else:
        # docstatus = 0 (Draft) — already in correct state after insert
        actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
        return {
            "name": pi.name,
            "status": "synced",
            "docstatus": 0,
            "items_synced": actual_item_count,
            "items_expected": expected_item_count
        }


# -----------------------------------------------------
# MAIN SYNC
# -----------------------------------------------------

@frappe.whitelist()
def sync_external_purchase_invoice_docs(source_doctype, names):
    """
    Main sync entry point.

    Each document is processed sequentially within a single transaction.
    Frappe automatically commits the transaction at HTTP request end.

    Per-document errors are caught and logged — they do NOT roll back
    previously processed documents because InnoDB only rolls back the
    failing statement (not the whole transaction) on constraint errors.
    """

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            result = _sync_single_purchase_invoice(source_doctype, name)
            results.append(result)
            frappe.db.set_value(
                source_doctype,
                name,
                "is_synced",
                1,
                update_modified=False
            )
 

        except Exception as e:
            error = frappe.get_traceback()

            _log(
                title=f"Purchase Invoice Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results