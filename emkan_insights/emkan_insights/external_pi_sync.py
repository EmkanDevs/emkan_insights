import frappe
import json
from frappe.utils import cint, flt, getdate
from erpnext.accounts.party import get_due_date_from_template
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.override.accounts_controller import (
    custom_validate_company_linked_addresses,
)
from emkan_insights.emkan_insights.sync_guard import (
    copied_link_validation_disabled,
    set_source_site_fields,
    sync_record_guard,
    temporarily_enable_transaction_items,
)


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

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


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
# CANCEL HELPER
# -----------------------------------------------------

def _safe_cancel_doc(doc, log_prefix=""):
    """
    Cancel a document via the ORM (doc.cancel()).
    Raises immediately on failure — no raw-SQL fallback that could leave
    GL/SLE data in an inconsistent state.
    Returns True if successful.
    """
    doc.flags.ignore_permissions = True
    with temporarily_enable_transaction_items(doc):
        doc.cancel()  # ORM cancel — triggers all on_cancel hooks (GL, SLE, etc.)
    _log(f"{log_prefix} cancelled", "")
    return True


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


def _external_pi_candidates(remote_name):
    remote_name = (remote_name or "").strip()
    if not remote_name:
        return []
    abbr = frappe.db.get_value("Company", frappe.db.get_default("company"), "abbr") or ""
    candidates = [remote_name]
    if not remote_name.startswith(f"{abbr}-"):
        candidates.insert(0, f"{abbr}-{remote_name}")
    return candidates


def _resolve_external_purchase_invoice(remote_name):
    for candidate in _external_pi_candidates(remote_name):
        if frappe.db.exists("External Purchase Invoice", candidate):
            return candidate

    meta = frappe.get_meta("External Purchase Invoice")
    if meta.has_field("remote_id"):
        return frappe.db.get_value(
            "External Purchase Invoice",
            {"remote_id": ["in", _external_pi_candidates(remote_name)]},
            "name",
        )

    return None


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

    candidates = _external_pi_candidates(remote_or_local_pinv)
    candidates.append(remote_or_local_pinv)

    for candidate in dict.fromkeys(candidates):
        local_name = frappe.db.get_value(
            "Purchase Invoice", {"remote_id": candidate}, "name"
        )
        if local_name:
            return local_name

        if frappe.db.exists("Purchase Invoice", candidate):
            return candidate

        prefixed = _build_company_prefixed_name(company_abbr, candidate)
        if prefixed != candidate and frappe.db.exists("Purchase Invoice", prefixed):
            return prefixed

    return None


def _resolve_submitted_local_pinv(remote_name, company_abbr):
    local_name = resolve_local_pinv(remote_name, company_abbr)
    if local_name and cint(
        frappe.db.get_value("Purchase Invoice", local_name, "docstatus")
    ) == 1:
        return local_name
    return None


def _resolve_cancelled_local_pinv(remote_name, company_abbr):
    local_name = resolve_local_pinv(remote_name, company_abbr)
    if local_name and cint(
        frappe.db.get_value("Purchase Invoice", local_name, "docstatus")
    ) == 2:
        return local_name
    return None

def get_po_detail(purchase_order, item_code, remote_po_item=None):
    if not purchase_order:
        return None

    if remote_po_item:
        if frappe.db.exists(
            "Purchase Order Item",
            {"name": remote_po_item, "parent": purchase_order},
        ):
            return remote_po_item

        po_item_meta = frappe.get_meta("Purchase Order Item")
        if po_item_meta.has_field("custom_remote_id"):
            local_po_item = frappe.db.get_value(
                "Purchase Order Item",
                {
                    "parent": purchase_order,
                    "custom_remote_id": remote_po_item,
                },
                "name",
            )
            if local_po_item:
                return local_po_item

    return frappe.db.get_value(
        "Purchase Order Item",
        {"parent": purchase_order, "item_code": item_code},
        "name",
    )


def _sync_pi_item_with_po_reference(item_row):
    """Copy fields that ERPNext validates against the linked PO item."""
    po_detail = item_row.get("po_detail")
    if not po_detail:
        return

    po_values = frappe.db.get_value(
        "Purchase Order Item",
        po_detail,
        ["project", "item_code", "uom"],
        as_dict=True,
    )
    if not po_values:
        return

    item_row["project"] = po_values.get("project")
    if po_values.get("item_code"):
        item_row["item_code"] = po_values.item_code
    if po_values.get("uom"):
        item_row["uom"] = po_values.uom


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


def get_returned_pi_item(return_against, item_code, remote_pi_item=None):
    """
    Resolve the local Purchase Invoice Item row referenced by a return item.

    Source rows carry purchase_invoice_item as the remote child-row name. On
    this site ERPNext validates against the local returned-against PI item
    `name`, so using the remote value directly causes "Returned Item ... does
    not exist in Purchase Invoice ..." at submit time.
    """
    if not return_against:
        return None

    pi_item_meta = frappe.get_meta("Purchase Invoice Item")

    if remote_pi_item and pi_item_meta.has_field("custom_remote_id"):
        local_pi_item = frappe.db.get_value(
            "Purchase Invoice Item",
            {"parent": return_against, "custom_remote_id": remote_pi_item},
            "name"
        )
        if local_pi_item:
            return local_pi_item

    candidates = frappe.get_all(
        "Purchase Invoice Item",
        filters={"parent": return_against, "item_code": item_code},
        pluck="name",
    )

    if len(candidates) == 1:
        return candidates[0]

    return None


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

    def _dedupe_table_by_remote_id(doctype, parentfield):
        """
        Dedupe by custom_remote_id only.

        Each remote item row has a globally-unique name. When we sync, that
        name is stored in custom_remote_id on the local row. If two local
        rows share the same custom_remote_id they are true duplicates (e.g.
        from a retry). Rows with different custom_remote_id are ALWAYS
        distinct — even if every other field (item_code, qty, rate, etc.)
        happens to match — because they represent different lines on the
        original invoice that may link to different PO items, MR items, etc.

        Rows without a custom_remote_id are left alone (safer than guessing).
        """
        if not frappe.db.exists("DocType", doctype):
            return

        # Check if the custom_remote_id field exists on this doctype
        if not frappe.get_meta(doctype).has_field("custom_remote_id"):
            return

        rows = frappe.db.get_all(
            doctype,
            filters={
                "parent": pi_name,
                "parenttype": "Purchase Invoice",
                "parentfield": parentfield,
            },
            fields=["name", "custom_remote_id"],
            order_by="creation asc",
        )

        seen_remote_ids = set()
        delete_rows = []

        for row in rows:
            remote_id = (row.get("custom_remote_id") or "").strip()
            if remote_id:
                if remote_id in seen_remote_ids:
                    delete_rows.append(row.name)
                else:
                    seen_remote_ids.add(remote_id)
            # Rows without custom_remote_id are never deleted

        if delete_rows:
            frappe.db.delete(
                doctype,
                {"name": ["in", delete_rows]}
            )

    def _dedupe_table_by_fields(doctype, parentfield, key_fields, float_fields=()):
        """
        Field-based deduplication for tables that don't have custom_remote_id
        (taxes, payment_schedule). Keeps first occurrence, deletes rest.
        """
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

    # ITEMS: dedupe by custom_remote_id ONLY
    # This is the critical fix — field-based deduplication was incorrectly
    # removing legitimate duplicate item lines (same item_code/qty/rate but
    # different po_detail / material_request_item links).
    _dedupe_table_by_remote_id("Purchase Invoice Item", "items")

    # TAXES: field-based deduplication is safe here (no custom_remote_id,
    # and genuinely duplicate tax rows are almost always a sync artifact).
    _dedupe_table_by_fields(
        "Purchase Taxes and Charges", "taxes",
        ["charge_type", "account_head", "description"],
        ["rate", "tax_amount"],
    )

    # PAYMENT SCHEDULE: field-based deduplication is safe here.
    _dedupe_table_by_fields(
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
        if field in {
            "remote_id",
            "is_return",
            "return_against",
            "amended_from",
        }:
            continue
        if field in target_table_fields:
            continue
        if hasattr(target_doc, field):
            # Preserve exact value including negative for returns
            target_doc.set(field, value)


def _sanitize_purchase_invoice_header(pi):
    """Clear copied tree values that do not exist on the target site."""
    abbr = frappe.db.get_value("Company", pi.company, "abbr") or ""
    if pi.get("supplier_group") and not pi.supplier_group.startswith(f"{abbr}-"):
        pi.supplier_group = f"{abbr}-{pi.supplier_group}"

    if pi.get("supplier_group") and not frappe.db.exists(
        "Supplier Group", pi.supplier_group
    ):
        _log(
            f"PI {pi.name}: invalid supplier group cleared",
            f"Supplier Group '{pi.supplier_group}' does not exist locally.",
        )
        pi.supplier_group = None


def _clear_invalid_company_addresses(pi):
    """Clear copied billing/shipping addresses not linked to this company."""
    custom_validate_company_linked_addresses(pi)


def _set_source_posting_datetime(pi, ext_pi):
    """Preserve the source accounting date and time during ORM validation."""
    if not ext_pi.get("posting_date"):
        frappe.throw(f"External Purchase Invoice {ext_pi.name} has no posting date")

    pi.set_posting_time = 1
    pi.posting_date = ext_pi.posting_date
    if ext_pi.get("posting_time"):
        pi.posting_time = ext_pi.posting_time


def _normalize_payment_schedule_dates(pi):
    """Cap copied due dates at the limit allowed by the local template."""
    template = pi.get("payment_terms_template")
    if not template:
        return

    allowed_due_date = get_due_date_from_template(
        template,
        pi.get("posting_date"),
        pi.get("bill_date"),
    )
    if not allowed_due_date:
        return

    allowed_due_date = getdate(allowed_due_date)
    for row in pi.get("payment_schedule") or []:
        if row.get("due_date") and getdate(row.due_date) > allowed_due_date:
            row.due_date = allowed_due_date

    schedule_dates = [
        getdate(row.due_date)
        for row in pi.get("payment_schedule") or []
        if row.get("due_date")
    ]
    pi.due_date = max(schedule_dates) if schedule_dates else allowed_due_date


def _order_purchase_invoices_for_sync(names):
    """Order originals, amendments, then returns while preserving each group."""
    if not names:
        return []

    rows = frappe.get_all(
        "External Purchase Invoice",
        filters={"name": ["in", names]},
        fields=["name", "is_return", "return_against", "amended_from"],
    )
    row_by_name = {row.name: row for row in rows}

    originals = []
    amendments = []
    returns = []
    for name in names:
        row = row_by_name.get(name)
        if row and (cint(row.is_return) or row.return_against):
            returns.append(name)
        elif row and row.amended_from:
            amendments.append(name)
        else:
            originals.append(name)

    return originals + amendments + returns


def _resolve_effective_external_pi_return_parent(ext_return, external_parent):
    """Use an active matching amendment instead of its cancelled original."""
    if not external_parent:
        return None

    current_parent = external_parent
    visited = set()
    while current_parent and current_parent not in visited:
        visited.add(current_parent)
        amendments = frappe.get_all(
            "External Purchase Invoice",
            filters={"amended_from": current_parent},
            fields=["name", "docstatus", "supplier", "company", "currency"],
            order_by="creation desc",
        )
        if not amendments:
            break

        for amendment in amendments:
            if (
                cint(amendment.docstatus) != 2
                and amendment.supplier == ext_return.get("supplier")
                and amendment.company == ext_return.get("company")
                and (
                    not amendment.currency
                    or not ext_return.get("currency")
                    or amendment.currency == ext_return.get("currency")
                )
            ):
                return amendment.name

        current_parent = amendments[0].name

    return external_parent


def _has_pending_cancelled_pi_return(remote_parent):
    return bool(
        frappe.db.exists(
            "External Purchase Invoice",
            {
                "return_against": ["in", _external_pi_candidates(remote_parent)],
                "docstatus": 2,
            },
        )
    )


def _sync_pi_amendment_parent(
    source_doctype, ext_pi, company_abbr, dependency_stack
):
    remote_parent = ext_pi.get("amended_from")
    local_parent = _resolve_cancelled_local_pinv(remote_parent, company_abbr)
    if local_parent:
        return local_parent

    external_parent = _resolve_external_purchase_invoice(remote_parent)
    if not external_parent or external_parent in dependency_stack:
        return None

    _sync_single_purchase_invoice(
        source_doctype,
        external_parent,
        dependency_stack=(*dependency_stack, ext_pi.name),
        force_cancellation=True,
    )
    return _resolve_cancelled_local_pinv(remote_parent, company_abbr)


def _sync_pi_return_parent(source_doctype, ext_pi, company_abbr, dependency_stack):
    external_parent = _resolve_external_purchase_invoice(ext_pi.get("return_against"))
    effective_parent = _resolve_effective_external_pi_return_parent(
        ext_pi, external_parent
    )
    parent_to_resolve = effective_parent or ext_pi.get("return_against")
    local_parent = _resolve_submitted_local_pinv(parent_to_resolve, company_abbr)

    if not local_parent:
        if not effective_parent or effective_parent in dependency_stack:
            return None, False
        _sync_single_purchase_invoice(
            source_doctype,
            effective_parent,
            dependency_stack=(*dependency_stack, ext_pi.name),
            defer_cancellation=True,
        )
        local_parent = _resolve_submitted_local_pinv(
            parent_to_resolve, company_abbr
        )

    cancel_parent_after = bool(
        local_parent
        and cint(ext_pi.docstatus) == 2
        and cint(
            frappe.db.get_value(
                "External Purchase Invoice", effective_parent, "docstatus"
            )
        ) == 2
    )
    return local_parent, cancel_parent_after


def _cancel_submitted_pi_parent(parent_name, return_name):
    if cint(frappe.db.get_value("Purchase Invoice", parent_name, "docstatus")) != 1:
        return

    parent = frappe.get_doc("Purchase Invoice", parent_name)
    _safe_cancel_doc(
        parent,
        log_prefix=f"PI {parent_name} after cancelled return {return_name}",
    )


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


def _normalize_payment_fields(pi, ext_pi):
    """Keep copied payment fields consistent with the recalculated total."""
    if not flt(ext_pi.get("write_off_amount")):
        pi.write_off_amount = 0
        pi.base_write_off_amount = 0
        pi.write_off_account = None
        pi.write_off_cost_center = None

    if not cint(pi.get("is_paid")):
        pi.paid_amount = 0
        pi.base_paid_amount = 0
        return

    source_total = flt(
        ext_pi.get("rounded_total") or ext_pi.get("grand_total")
    )
    source_paid = flt(ext_pi.get("paid_amount"))
    source_precision = ext_pi.precision("paid_amount")
    source_tolerance = 1 / (10 ** source_precision)

    # A fully-paid source invoice must remain fully paid after ERPNext
    # recalculates local item and tax totals. Otherwise a rounding difference
    # can make paid_amount alone exceed grand_total and validate_cash() reports
    # the misleading "Paid amount + Write Off Amount" error.
    if source_total and abs(source_paid - source_total) <= source_tolerance:
        target_total = flt(
            pi.get("rounded_total") or pi.get("grand_total"),
            pi.precision("paid_amount"),
        )
        pi.paid_amount = target_total
        pi.base_paid_amount = flt(
            target_total * flt(pi.get("conversion_rate") or 1),
            pi.precision("base_paid_amount"),
        )


# -----------------------------------------------------
# INSERT ITEMS ONE-BY-ONE WITH ERROR ISOLATION
# -----------------------------------------------------

def _insert_pi_items_safely(
    pi_name,
    ext_pi,
    company_abbr=None,
    is_return=None,
    local_return_against=None,
):
    ...
    success_count = 0
    errors = []
    target_item_meta = frappe.get_meta("Purchase Invoice Item")
    if is_return is None:
        is_return = cint(ext_pi.get("is_return", 0))
    else:
        is_return = cint(is_return)

    if not local_return_against and is_return and ext_pi.get("return_against"):
        local_return_against = resolve_local_pinv(ext_pi.return_against, company_abbr)

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
                             "material_request_item", "purchase_invoice_item"}:
                    continue
                # IMPORTANT: Copy value AS-IS to preserve negative qty for returns
                # Do NOT use abs() or any transformation here
                item_row[field] = value

            # Resolve PO
            if row.purchase_order:
                local_po = resolve_local_po(row.purchase_order)
                item_row["purchase_order"] = local_po
                item_row["po_detail"] = (
                    get_po_detail(
                        local_po,
                        row.item_code,
                        remote_po_item=(
                            row.get("po_detail")
                            or row.get("purchase_order_item")
                        ),
                    )
                    if local_po
                    else None
                )
                _sync_pi_item_with_po_reference(item_row)
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

            # Resolve purchase_invoice_item for return invoice validation.
            if is_return and local_return_against:
                remote_pi_item = row.get("purchase_invoice_item") or row.get("name")
                item_row["purchase_invoice_item"] = get_returned_pi_item(
                    local_return_against,
                    row.item_code,
                    remote_pi_item=remote_pi_item
                )
            else:
                item_row["purchase_invoice_item"] = None

            if target_item_meta.has_field("custom_remote_id"):
                item_row["custom_remote_id"] = row.name

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

def _sync_existing_docstatus(existing_name, ext_pi, defer_cancellation=False):
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
                _normalize_payment_fields(pi, ext_pi)
                _normalize_payment_schedule_dates(pi)
                with temporarily_enable_transaction_items(pi):
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
                _normalize_payment_fields(pi, ext_pi)
                _normalize_payment_schedule_dates(pi)
                with temporarily_enable_transaction_items(pi):
                    pi.submit()
                pi = frappe.get_doc("Purchase Invoice", existing_name)

            if defer_cancellation:
                return {
                    "name": pi.name,
                    "status": "synced_pending_cancel",
                    "docstatus": 1,
                }

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
        raise


# -----------------------------------------------------
# SINGLE DOCUMENT SYNC
# -----------------------------------------------------

def _sync_single_purchase_invoice(
    source_doctype,
    name,
    dependency_stack=(),
    defer_cancellation=False,
    force_cancellation=False,
):
    """
    Process a single Purchase Invoice sync.
    All database writes (insert/save/submit/cancel/db_set/sql) happen within
    the current transaction. Frappe commits automatically at request end.
    """
    ext_pi = frappe.get_doc(source_doctype, name)
    sync_started_at = ext_pi.modified
    remote_id = ext_pi.name
    target_docstatus = cint(ext_pi.docstatus)
    defer_source_cancellation = target_docstatus == 2 and (
        not force_cancellation
        and (
            defer_cancellation
            or _has_pending_cancelled_pi_return(remote_id)
        )
    )

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
            raise Exception(
                f"PI {existing}: item count mismatch on non-draft Purchase Invoice "
                "(manual rebuild needed)."
            )
        else:
            result = _sync_existing_docstatus(
                existing,
                ext_pi,
                defer_cancellation=defer_source_cancellation,
            )

            if result.get("status") in ("exists", "docstatus_synced"):
                ext_pi.db_set(
                    "last_synced_on",
                    sync_started_at,
                    update_modified=False
                )

            return result

    # Clean up any orphaned child rows for the target name
    _delete_purchase_invoice_child_rows(target_name)

    pi = frappe.new_doc("Purchase Invoice")

    # Set name BEFORE anything else
    pi.remote_id = remote_id
    pi.name = target_name
    pi.flags.name_set = True

    set_source_site_fields(pi, site_url=getattr(ext_pi, "source_site", None))

    # Copy scalar header fields only
    _copy_header_fields(ext_pi, pi)
    _sanitize_purchase_invoice_header(pi)
    _clear_invalid_company_addresses(pi)
    if pi.get("credit_to"):
        pi.credit_to = resolve_company_account(pi.credit_to, ext_pi.company, company_abbr)
    _set_source_posting_datetime(pi, ext_pi)

    # Fix currency
    if not pi.currency:
        pi.currency = frappe.get_cached_value(
            "Company", pi.company, "default_currency"
        )

    items_have_negative_qty = any(
        flt(row.get("qty")) < 0 for row in (ext_pi.get("items") or [])
    )
    is_return_effective = cint(ext_pi.get("is_return", 0)) or items_have_negative_qty

    if ext_pi.get("amended_from"):
        pi.amended_from = _sync_pi_amendment_parent(
            source_doctype,
            ext_pi,
            company_abbr,
            dependency_stack,
        )
        if not pi.amended_from:
            frappe.throw(
                f"Cannot sync amendment '{name}' because cancelled Purchase "
                f"Invoice '{ext_pi.amended_from}' is unavailable."
            )
    else:
        pi.amended_from = None

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
    cancel_return_parent_after_sync = False
    if is_return_effective:
        deferred_is_return = 1
        (
            deferred_return_against,
            cancel_return_parent_after_sync,
        ) = _sync_pi_return_parent(
            source_doctype,
            ext_pi,
            company_abbr,
            dependency_stack,
        )
        if not deferred_return_against:
            frappe.throw(
                f"Cannot sync return '{name}' because submitted Purchase Invoice "
                f"'{ext_pi.return_against}' is unavailable."
            )

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
            if tax_row.get("account_head"):
                tax_row["account_head"] = resolve_company_account(
                    tax_row["account_head"], ext_pi.company, company_abbr
                )
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

    _normalize_payment_schedule_dates(pi)

    # FLAGS
    # items is mandatory (reqd=1) on Purchase Invoice, but we insert the
    # parent first and attach item rows via db_insert() afterwards. Without
    # ignore_mandatory, insert() throws "Data missing in table: Items".
    pi.flags.ignore_permissions = True
    pi.flags.ignore_naming_series = True

    # INSERT PARENT ONLY (no items yet) — ORM insert writes to DB
    with temporarily_enable_transaction_items(pi):
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
        pi.name,
        ext_pi,
        company_abbr,
        is_return=is_return_effective,
        local_return_against=deferred_return_against,
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
        raise Exception(
            f"PI {pi.name}: external document has zero items; submit skipped "
            "to avoid 'Data missing in table: Items'."
        )

    if actual_item_count != expected_item_count:
        _log(
            title=f"PI Sync - items mismatch for {pi.name}",
            message=f"Expected {expected_item_count} items, "
                    f"found {actual_item_count} after insert. "
                    f"Successfully inserted: {success_count}, "
                    f"Errors: {len(item_errors)}. "
                    f"Aborting submit to prevent GL entry count divergence."
        )
        # Aborting submit to prevent GL entry count divergence.
        # Do NOT submit — submitting with partial items creates the wrong
        # number of GL entries (root cause of the 2875 vs 2847 mismatch).
        raise Exception(
            f"PI {pi.name}: item count mismatch ({actual_item_count}/{expected_item_count}); "
            f"submit aborted to prevent GL entry divergence. "
            f"Item errors: {'; '.join(item_errors) if item_errors else 'none'}."
        )

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

    if target_docstatus == 1:
        # Submit via ORM
        try:
            _recalculate_totals(pi)
            _normalize_payment_fields(pi, ext_pi)
            _normalize_payment_schedule_dates(pi)
            with temporarily_enable_transaction_items(pi):
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
            raise

    elif target_docstatus == 2:
        # Submit first via ORM, then cancel
        try:
            _recalculate_totals(pi)
            _normalize_payment_fields(pi, ext_pi)
            _normalize_payment_schedule_dates(pi)
            with temporarily_enable_transaction_items(pi):
                pi.submit()
            _safe_dedupe(pi.name)
            _log(f"PI {pi.name} submitted (step 1 of cancel)", "")

            if defer_source_cancellation:
                return {
                    "name": pi.name,
                    "status": "synced_pending_cancel",
                    "docstatus": 1,
                    "items_synced": actual_item_count,
                    "items_expected": expected_item_count,
                }

            # Re-fetch fresh doc and cancel
            pi = frappe.get_doc("Purchase Invoice", pi.name)
            with temporarily_enable_transaction_items(pi):
                _safe_cancel_doc(pi, log_prefix=f"PI {pi.name}")
            _safe_dedupe(pi.name)
            
            # Verify
            final_docstatus = frappe.db.get_value("Purchase Invoice", pi.name, "docstatus")
            final_status = frappe.db.get_value("Purchase Invoice", pi.name, "status")
            
            _log(f"PI {pi.name} cancel complete",
                 f"Final docstatus: {final_docstatus}, Final status: {final_status}")
            
            actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
            
            if final_docstatus == 2:
                if cancel_return_parent_after_sync and deferred_return_against:
                    _cancel_submitted_pi_parent(
                        deferred_return_against, pi.name
                    )
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
                raise Exception(
                    f"PI {pi.name}: cancel verification failed - docstatus is {final_docstatus}"
                )

        except Exception as cancel_err:
            tb = frappe.get_traceback()
            _log(f"PI {pi.name} SUBMIT+CANCEL FAILED",
                 f"Error: {str(cancel_err)}\n\nTraceback:\n{tb}")
            raise


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_purchase_invoice_docs(source_doctype, names, sync_now=False):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    ordered_names = _order_purchase_invoices_for_sync(unique_names)

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            ordered_names,
            CHUNK_SIZE,
            lambda chunk: _sync_purchase_invoice_docs_job(
                source_doctype=source_doctype,
                names=chunk,
            ),
        )

    queued_chunks = (len(ordered_names) + CHUNK_SIZE - 1) // CHUNK_SIZE
    if ordered_names:
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_pi_sync._sync_purchase_invoice_docs_job",
            queue="long",
            job_name=f"pi_sync_chunk_{frappe.generate_hash(length=8)}_0",
            timeout=6000,
            source_doctype=source_doctype,
            names=ordered_names,
            batch_index=0,
            enqueue_after_commit=True,
        )

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def _sync_purchase_invoice_docs_job(source_doctype, names, batch_index=0):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    batch_index = cint(batch_index)
    if batch_index == 0:
        names = _order_purchase_invoices_for_sync(list(names))
    chunk = names[batch_index * CHUNK_SIZE:(batch_index + 1) * CHUNK_SIZE]
    results = []

    # Copied header links (address / contact / terms) reference remote
    # records that may not exist here or belong to a different local party.
    # Skip those validators for the whole chunk so insert/submit/cancel
    # don't abort on them.
    with copied_link_validation_disabled():
        for name in chunk:
            try:
                with sync_record_guard(source_doctype, name):
                    result = _sync_single_purchase_invoice(source_doctype, name)
                    results.append(result)
            except Exception as e:
                log_record_failure(source_doctype, name, str(e))
                raise

    next_batch_index = batch_index + 1
    if next_batch_index * CHUNK_SIZE < len(names):
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_pi_sync._sync_purchase_invoice_docs_job",
            queue="long",
            job_name=(
                f"pi_sync_chunk_{frappe.generate_hash(length=8)}_"
                f"{next_batch_index}"
            ),
            timeout=6000,
            source_doctype=source_doctype,
            names=names,
            batch_index=next_batch_index,
            enqueue_after_commit=True,
        )

    return results
