import frappe
import json
from frappe.utils import flt
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import sync_record_guard

# ==========================================================
# CONSTANTS
# ==========================================================

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

EXPLICITLY_HANDLED_HEADER_FIELDS = {
    "references", "taxes", "deductions", "remote_id", "custom_remote_id"
}

LOCAL_PARENTTYPE = "Payment Entry"

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300

# Fields we deliberately DO NOT copy from the remote reference row.
# payment_term: term-level outstanding is recomputed locally and has no
#   guaranteed parity with the remote system's schedule state -> causes
#   "Cannot allocate more than X against payment term Y" on submit.
REFERENCE_FIELDS_TO_DROP = {"payment_term"}


# ==========================================================
# LOGGING (safe title length)
# ==========================================================

def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


# ==========================================================
# NAME / COMPANY HELPERS
# ==========================================================

def _get_company_abbr(company):
    if not company:
        return ""
    return frappe.db.get_value("Company", company, "abbr") or ""


def _build_target_name(company_abbr, remote_id):
    """Canonical local PE name: {company_abbr}-{remote_id} (no double-prefix)."""
    remote_id = (remote_id or "").strip()
    if not remote_id:
        return remote_id
    if company_abbr and remote_id.startswith(f"{company_abbr}-"):
        return remote_id
    return f"{company_abbr}-{remote_id}" if company_abbr else remote_id


def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None, company=None):
    """
    Resolve a linked local document for PE references.

    1. Prefixed local name: {company_abbr}-{remote_name}
    2. remote_id / custom_remote_id lookup (company-scoped when possible)
    3. Raw name existence check
    """
    if not remote_or_local_name or not doctype:
        return None

    if not frappe.db.exists("DocType", doctype):
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    meta = frappe.get_meta(doctype)
    for remote_field in ("remote_id", "custom_remote_id"):
        if not meta.has_field(remote_field):
            continue
        filters = {remote_field: remote_or_local_name}
        if company and meta.has_field("company"):
            filters["company"] = company
        local_name = frappe.db.get_value(doctype, filters, "name")
        if local_name:
            return local_name
        # fallback without company if scoped lookup misses
        if company and meta.has_field("company"):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def _find_existing_payment_entry(remote_id, target_name, company):
    """
    Find an already-synced local Payment Entry for this remote id.

    Scoped by company so CPC ACC-PAY-2026-00001 does not collide with an
    SEC / IMC Payment Entry that happens to share the same remote name.
    """
    if remote_id:
        filters = {"remote_id": remote_id}
        if company:
            filters["company"] = company
        existing = frappe.db.get_value("Payment Entry", filters, "name")
        if existing:
            return existing

    if target_name and frappe.db.exists("Payment Entry", target_name):
        if not company or frappe.db.get_value("Payment Entry", target_name, "company") == company:
            return target_name

    return None


# ==========================================================
# ACCOUNT RESOLUTION (taxes / deductions)
# ==========================================================

def _resolve_account(account, company, company_abbr):
    """
    Resolve a possibly-remote account name to a valid local one.
    make_gl_entries() silently skips any row whose account doesn't exist
    locally -- no exception, no log, just a missing GL leg. This is the
    single biggest cause of "fewer GL rows locally than on source" bugs.
    """
    if not account:
        return None

    return resolve_company_account(account, company, company_abbr)


def _resolve_amended_from(amended_from, company_abbr, company, target_name):
    local_amended_from = resolve_local_doc(
        "Payment Entry", amended_from, company_abbr=company_abbr, company=company
    )
    if local_amended_from:
        return local_amended_from

    _log(
        title=f"PE {target_name} amended_from unresolved - cleared",
        message=(
            f"Could not resolve amended_from '{amended_from}' for Payment Entry "
            f"{target_name}. Cleared the link before insert to avoid a dangling "
            f"Amended From validation failure."
        ),
    )
    return None


# ==========================================================
# REFERENCE ROW SANITIZATION
# ==========================================================

def _build_reference_row(row, company_abbr, company, remote_id, target_name):
    """
    Build a sanitized Payment Entry Reference row, or return None if the
    reference cannot be safely resolved (row should be dropped, not
    appended with a dangling/garbage value).
    """
    ref_doctype = row.get("reference_doctype")
    ref_name = row.get("reference_name")

    local_ref = None
    if ref_doctype and ref_name:
        local_ref = resolve_local_doc(
            ref_doctype, ref_name, company_abbr=company_abbr, company=company
        )
        if not local_ref:
            _log(
                title=f"PE {target_name} reference unresolved - row dropped",
                message=(
                    f"External Payment Entry {remote_id}: could not resolve "
                    f"{ref_doctype} '{ref_name}' to a local document "
                    f"(tried {company_abbr}-{ref_name}, remote_id lookup, and raw name). "
                    f"Row DROPPED rather than appended with a dangling reference_name - "
                    f"appending it would let the PE submit successfully while silently "
                    f"posting fewer GL entries than the source, and the allocated amount "
                    f"for this reference would go unaccounted for. Verify total allocation "
                    f"still matches the source PE after sync."
                ),
            )
            return None

    ref_row = {}
    for field, value in row.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field in REFERENCE_FIELDS_TO_DROP:
            continue
        ref_row[field] = value

    ref_row["reference_name"] = local_ref

    if row.get("payment_term"):
        _log(
            title=f"PE {target_name} payment_term dropped on reference",
            message=(
                f"Reference {ref_doctype}/{local_ref}: remote payment_term "
                f"'{row.get('payment_term')}' dropped intentionally. Term-level "
                f"outstanding is recomputed locally and has no guaranteed parity "
                f"with the source system's schedule state; keeping it causes "
                f"'Cannot allocate more than X against payment term Y' on submit. "
                f"Allocation now validates against invoice-level outstanding instead."
            ),
        )

    return ref_row


# ==========================================================
# EXCHANGE RATE NORMALIZATION
# ==========================================================

def _normalize_exchange_fields(doc):
    """
    Force same-currency exchange rates to 1 / zero out spurious difference
    amounts instead of trusting remote-stored values verbatim.

    Blindly copying source_exchange_rate / target_exchange_rate /
    difference_amount from the remote system (with no recalculation step,
    unlike the PI sync's _recalculate_totals) triggers ERPNext's exchange
    gain/loss posting on submit even for a pure SAR-to-SAR entry, and fails
    with "Please set default Exchange Gain/Loss Account" if the company
    has none configured.
    """
    paid_from_currency = frappe.db.get_value("Account", doc.paid_from, "account_currency") if doc.paid_from else None
    paid_to_currency = frappe.db.get_value("Account", doc.paid_to, "account_currency") if doc.paid_to else None

    same_currency_txn = (
        paid_from_currency and paid_to_currency and paid_from_currency == paid_to_currency
    )

    if same_currency_txn:
        if doc.get("source_exchange_rate") != 1 or doc.get("target_exchange_rate") != 1:
            _log(
                title=f"PE {doc.name} exchange rate normalized",
                message=(
                    f"Same-currency transaction ({paid_from_currency}) had "
                    f"source_exchange_rate={doc.get('source_exchange_rate')}, "
                    f"target_exchange_rate={doc.get('target_exchange_rate')} copied "
                    f"verbatim from remote. Forced both to 1 and difference_amount "
                    f"to 0 to avoid spurious exchange gain/loss posting on submit."
                ),
            )
        doc.source_exchange_rate = 1
        doc.target_exchange_rate = 1
        doc.difference_amount = 0

        for ref in doc.get("references") or []:
            if ref.get("exchange_rate") not in (None, 1):
                ref.exchange_rate = 1
            if ref.get("exchange_gain_loss"):
                ref.exchange_gain_loss = 0


# ==========================================================
# GL VERIFICATION
# ==========================================================

def _log_gl_entry_verification(pe_name):
    gl_entries = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": "Payment Entry", "voucher_no": pe_name, "is_cancelled": 0},
        fields=["account", "cost_center", "debit", "credit"],
    )
    if not gl_entries:
        _log(
            title=f"PE {pe_name} submitted with ZERO GL entries",
            message=(
                "Submit succeeded but no GL entries were created. Check account "
                "resolution on paid_from/paid_to, and party/reference resolution."
            ),
        )
    return gl_entries


# ==========================================================
# SAFE CANCEL (with fallback + GL reversal)
# ==========================================================

def _cancel_gl_entries_for_voucher(doctype, docname):
    existing_gl_entries = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": doctype, "voucher_no": docname, "is_cancelled": 0},
        fields=["*"],
    )
    if not existing_gl_entries:
        return
    try:
        from erpnext.accounts.general_ledger import make_reverse_gl_entries
        make_reverse_gl_entries(existing_gl_entries)
        _log(
            f"GL entries reversed for {doctype} {docname}",
            f"Reversed {len(existing_gl_entries)} GL entry row(s).",
        )
    except ImportError:
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


def _force_cancel_pe(docname):
    """
    INTENTIONALLY DISABLED: Force-setting docstatus via raw SQL bypasses all
    ERPNext lifecycle hooks. Even though GL reversal is attempted before the
    SQL flip, the cancel() ORM path also handles Payment Entry References,
    party ledger updates, and other side effects that raw SQL cannot replicate.

    This stub is kept so any remaining call sites surface a clear error.
    Use _safe_cancel_pe() (ORM cancel) instead.
    """
    raise frappe.ValidationError(
        f"_force_cancel_pe('{docname}') is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks. "
        f"Use the ORM cancel() path."
    )


def _safe_cancel_pe(doc):
    """
    Cancel a Payment Entry using the ORM (doc.cancel()), which correctly runs
    all ERPNext lifecycle hooks including GL Entry reversal and party ledger
    updates.

    If ORM cancel fails, the exception is logged as CRITICAL and re-raised.
    Raw SQL force-cancel is intentionally NOT used as a fallback — it would
    leave Payment Entry References, party outstandings, and other state
    inconsistent even when GL entries are reversed first.
    """
    try:
        doc.flags.ignore_permissions = True
        doc.cancel()  # ORM cancel — runs all lifecycle hooks
        return
    except Exception as cancel_err:
        frappe.log_error(
            title=f"CRITICAL: PE {doc.name} ORM cancel failed — manual intervention required",
            message=(
                f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
                f"The Payment Entry has NOT been cancelled. Its GL Entries and party "
                f"ledger remain active. Investigate why cancel() failed and cancel the "
                f"document manually via ERPNext to ensure ledger integrity."
            )
        )
        raise


# ==========================================================
# DOCSTATUS RECONCILIATION FOR ALREADY-SYNCED RECORDS
# ==========================================================

def _sanitize_existing_reference_terms(pe_name):
    """
    For a draft PE already sitting in the DB (from a prior failed sync
    attempt), strip payment_term off its reference rows via direct SQL so
    a resubmit doesn't hit the same term-level allocation error again.
    """
    if not frappe.db.exists("DocType", "Payment Entry Reference"):
        return
    frappe.db.sql(
        """
        UPDATE `tabPayment Entry Reference`
        SET payment_term = NULL, modified = NOW()
        WHERE parent = %s AND parenttype = %s AND payment_term IS NOT NULL
        """,
        (pe_name, LOCAL_PARENTTYPE),
    )


def _repair_existing_payment_entry_accounts(pe_name, src, company, company_abbr):
    updates = {}
    for fieldname in ("paid_from", "paid_to"):
        source_account = getattr(src, fieldname, None)
        if not source_account:
            continue

        resolved = _resolve_account(source_account, company, company_abbr)
        if resolved:
            updates[fieldname] = resolved

    for fieldname, value in updates.items():
        frappe.db.set_value("Payment Entry", pe_name, fieldname, value, update_modified=False)

    child_tables = {
        "Advance Taxes and Charges": "account_head",
        "Payment Entry Deduction": "account",
    }
    for child_dt, account_field in child_tables.items():
        if not frappe.db.exists("DocType", child_dt):
            continue
        if not frappe.get_meta(child_dt).has_field(account_field):
            continue
        rows = frappe.get_all(
            child_dt,
            filters={"parent": pe_name, "parenttype": LOCAL_PARENTTYPE},
            fields=["name", account_field],
        )
        for row in rows:
            source_account = row.get(account_field)
            resolved = _resolve_account(source_account, company, company_abbr)
            if resolved and resolved != source_account:
                frappe.db.set_value(child_dt, row.name, account_field, resolved, update_modified=False)


def _sync_existing_docstatus(existing_name, src):
    """
    Reconcile a local Payment Entry that already exists but whose docstatus
    doesn't match the source. Without this, any PE that failed submit on a
    prior sync attempt (e.g. term-allocation or exchange-rate errors) is
    permanently stuck as a 0-GL draft, because _find_existing_payment_entry
    returns it as "already synced" on every later run without retrying.
    """
    local_docstatus = frappe.db.get_value("Payment Entry", existing_name, "docstatus")
    target_docstatus = src.docstatus
    company = src.company
    company_abbr = _get_company_abbr(company)
    _repair_existing_payment_entry_accounts(existing_name, src, company, company_abbr)

    if local_docstatus == target_docstatus:
        return existing_name

    # ----------------------------------------------------------
    # Submitted locally but should be Cancelled on source.
    #
    # Root cause this covers: the original sync path did
    #   doc.submit(); doc.cancel()
    # with no try/except around cancel(). If cancel() raised for any
    # reason (GL reversal conflict, link validation, etc.), submit()
    # had already succeeded one line earlier, so the record was left
    # permanently stuck as Submitted. Because the old dedupe check
    # returned any existing record unconditionally, that mismatch was
    # never retried on later sync runs either.
    # ----------------------------------------------------------
    if local_docstatus == 1 and target_docstatus == 2:
        try:
            doc = frappe.get_doc("Payment Entry", existing_name)
            doc.flags.ignore_permissions = True
            _safe_cancel_pe(doc)
            _log(f"PE {existing_name} docstatus synced: Submitted -> Cancelled", "")
            return existing_name
        except Exception:
            _log(
                title=f"PE {existing_name} Submitted->Cancelled sync FAILED",
                message=frappe.get_traceback(),
            )
            raise

    if local_docstatus != 0:
        _log(
            title=f"PE {existing_name} cannot transition from {local_docstatus} to {target_docstatus}",
            message="Only Draft->Submitted/Cancelled and Submitted->Cancelled are handled automatically.",
        )
        return existing_name

    try:
        _sanitize_existing_reference_terms(existing_name)

        doc = frappe.get_doc("Payment Entry", existing_name)
        doc.flags.ignore_permissions = True

        _normalize_exchange_fields(doc)
        doc.save(ignore_permissions=True)

        doc.submit()
        _log_gl_entry_verification(doc.name)

        if target_docstatus == 2:
            doc = frappe.get_doc("Payment Entry", doc.name)
            _safe_cancel_pe(doc)

        _log(f"PE {existing_name} docstatus synced: Draft -> {target_docstatus}", "")
        return doc.name

    except Exception:
        _log(
            title=f"PE {existing_name} docstatus reconciliation FAILED",
            message=frappe.get_traceback(),
        )
        raise


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_payment_entry_docs(source_doctype, names, sync_now=False):
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

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: _sync_payment_entry_docs_job(
                source_doctype=source_doctype,
                names=chunk,
            ),
        )

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"pe_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_payment_entry_sync._sync_payment_entry_docs_job",
            queue="long",
            job_name=job_name,
            timeout=6000,
            source_doctype=source_doctype,
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

def _sync_payment_entry_docs_job(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            with sync_record_guard(source_doctype, name):
                result = sync_external_payment_entry(source_doctype, name)
                results.append({"name": name, "status": "synced", "payment_entry": result})

        except Exception:
            # sync_record_guard has already rolled this record back.
            log_record_failure(source_doctype, name)
            raise

    return results


# ==========================================================
# MAIN SYNC FUNCTION
# ==========================================================

def sync_external_payment_entry(source_doctype, external_name):

    src = frappe.get_doc(source_doctype, external_name)
    remote_id = src.name
    company = src.company
    company_abbr = _get_company_abbr(company)
    target_name = _build_target_name(company_abbr, remote_id)

    # ------------------------------------------------------
    # Prevent duplicate (company-scoped) - now reconciles docstatus
    # instead of blindly returning a possibly-stuck draft.
    # ------------------------------------------------------
    existing = _find_existing_payment_entry(remote_id, target_name, company)
    if existing:
        return _sync_existing_docstatus(existing, src)

    # ------------------------------------------------------
    # Dependency Checks (Resolve Party FIRST)
    # ------------------------------------------------------
    local_party = None
    if src.party_type and src.party:
        local_party = resolve_local_doc(
            src.party_type, src.party, company_abbr=company_abbr, company=company
        )
        if not local_party:
            frappe.throw(f"{src.party_type} {src.party} not found locally. Sync master first.")

    local_paid_from = None

    if src.paid_from:
        local_paid_from = _resolve_account(src.paid_from, company, company_abbr)
        if not local_paid_from:
            frappe.throw(f"Account {src.paid_from} not found.")

    local_paid_to = None
    if src.paid_to:
        local_paid_to = _resolve_account(src.paid_to, company, company_abbr)
        if not local_paid_to:
            frappe.throw(f"Account {src.paid_to} not found.")

    # ------------------------------------------------------
    # Create Payment Entry with company-prefixed name
    # ------------------------------------------------------
    doc = frappe.new_doc("Payment Entry")
    doc.remote_id = remote_id
    doc.name = target_name
    doc.flags.name_set = True
    

    # ------------------------------------------------------
    # Copy Main Fields
    # ------------------------------------------------------
    for field, value in src.as_dict().items():
        if (
            field not in SYSTEM_FIELDS
            and field not in EXPLICITLY_HANDLED_HEADER_FIELDS
            and hasattr(doc, field)
        ):
            doc.set(field, value)

    # Ensure company + remote_id + resolved accounts stick after generic copy
    doc.book_advance_payments_in_separate_party_account = src.book_advance_payments_in_separate_party_account
    doc.company = company
    doc.remote_id = remote_id
    doc.paid_from = local_paid_from
    doc.paid_to = local_paid_to
    if getattr(src, "amended_from", None):
        doc.amended_from = _resolve_amended_from(src.amended_from, company_abbr, company, target_name)

    # ------------------------------------------------------
    # Apply Resolved Party Name
    # ------------------------------------------------------
    if local_party:
        doc.party = local_party

    # ------------------------------------------------------
    # References Table - resolve reference_name, drop payment_term
    # ------------------------------------------------------
    doc.set("references", [])

    for row in src.get("references") or []:
        ref_row = _build_reference_row(row, company_abbr, company, remote_id, target_name)
        # frappe.throw(str(ref_row))
        if ref_row is not None:
            doc.append("references", ref_row)

    if not doc.get("references") and (src.get("references")):
        _log(
            title=f"PE {target_name} - ALL references unresolved",
            message=(
                f"Every reference row on source PE {remote_id} failed to resolve. "
                f"This PE will submit as an on-account / unallocated payment rather "
                f"than being matched to its original invoice(s). Investigate before "
                f"relying on this record for reconciliation."
            ),
        )

    # ------------------------------------------------------
    # Taxes / Deductions - resolve account before copying
    # ------------------------------------------------------
    for table_field in ("taxes", "deductions"):
        if not hasattr(src, table_field) or not hasattr(doc, table_field):
            continue
        doc.set(table_field, [])
        for row in src.get(table_field) or []:
            child = {}
            for field, value in row.as_dict().items():
                if field not in SYSTEM_FIELDS:
                    child[field] = value

            if child.get("account"):
                resolved_account = _resolve_account(child["account"], company, company_abbr)
                if not resolved_account:
                    _log(
                        title=f"PE {target_name} {table_field} account unresolved",
                        message=(
                            f"Account '{child['account']}' not found locally for "
                            f"{table_field} row on source PE {remote_id}. Row appended "
                            f"with account cleared; it will silently drop its GL leg "
                            f"on submit unless a default resolves it."
                        ),
                    )
                child["account"] = resolved_account

            doc.append(table_field, child)

    # ------------------------------------------------------
    # Safe Flags
    # ------------------------------------------------------
     
    doc.flags.ignore_permissions = True
    doc.flags.ignore_naming_series = True

    # ------------------------------------------------------
    # Normalize exchange rate fields BEFORE insert
    # ------------------------------------------------------
    _normalize_exchange_fields(doc)

    # ------------------------------------------------------
    # Insert
    # ------------------------------------------------------
    doc.insert(
        ignore_permissions=True,
    )

    # ------------------------------------------------------
    # Naming safety net -> company-prefixed target_name
    # ------------------------------------------------------
    if doc.name != target_name:
        if not frappe.db.exists("Payment Entry", target_name):
            old_name = doc.name

            frappe.db.sql("""
                UPDATE `tabPayment Entry`
                SET name = %s
                WHERE name = %s
            """, (target_name, old_name))

            frappe.db.sql("""
                UPDATE `tabPayment Entry Reference`
                SET parent = %s
                WHERE parent = %s AND parenttype = %s
            """, (target_name, old_name, LOCAL_PARENTTYPE))

            for child_dt in ("Advance Taxes and Charges", "Payment Entry Deduction"):
                if frappe.db.exists("DocType", child_dt):
                    frappe.db.sql(f"""
                        UPDATE `tab{child_dt}`
                        SET parent = %s
                        WHERE parent = %s AND parenttype = %s
                    """, (target_name, old_name, LOCAL_PARENTTYPE))

            doc.name = target_name
        else:
            _log(
                title=f"PE Sync - name collision {doc.name} -> {target_name}",
                message="Could not rename because target name already exists.",
            )

    # Keep remote_id = external name (unprefixed source identity)
    frappe.db.set_value("Payment Entry", doc.name, "remote_id", remote_id, update_modified=False)

    # ------------------------------------------------------
    # Sync Docstatus
    # ------------------------------------------------------
    if src.docstatus == 1:
        try:
            doc = frappe.get_doc("Payment Entry", doc.name)
            doc.flags.ignore_permissions = True
            doc.submit()
            _log_gl_entry_verification(doc.name)
        except Exception:
            _log(
                title=f"PE {doc.name} SUBMIT FAILED",
                message=frappe.get_traceback(),
            )
            # Left as a draft (docstatus 0) intentionally. On the NEXT sync
            # run, _sync_existing_docstatus will find it, strip any leftover
            # payment_term values, and retry submit - it will not be
            # silently treated as "already synced" the way the old code did.
            return doc.name

    elif src.docstatus == 2:
        # Submit first (required before cancel creates reversal GL entries)
        try:
            doc = frappe.get_doc("Payment Entry", doc.name)
            doc.flags.ignore_permissions = True
            doc.submit()
            _log_gl_entry_verification(doc.name)
        except Exception:
            _log(
                title=f"PE {doc.name} SUBMIT FAILED (source wants Cancelled)",
                message=frappe.get_traceback(),
            )
            # Intentionally left as draft (docstatus 0). On next sync run,
            # _sync_existing_docstatus will find it, strip leftover
            # payment_term values, and retry the full submit→cancel flow.
            return doc.name

        # Cancel (only reached if submit succeeded above)
        try:
            doc = frappe.get_doc("Payment Entry", doc.name)
            _safe_cancel_pe(doc)
        except Exception:
            _log(
                title=f"PE {doc.name} CANCEL FAILED after successful submit",
                message=f"Doc is stuck at docstatus=1. Will retry cancel on next sync run.\n\n{frappe.get_traceback()}"
            )
            # Intentionally left at docstatus=1. On next sync run,
            # _sync_existing_docstatus will find it at local==1/target==2
            # and retry cancel via _safe_cancel_pe (including force-cancel fallback).
            return doc.name

    return doc.name
