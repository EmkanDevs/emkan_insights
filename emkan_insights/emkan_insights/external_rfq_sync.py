import frappe
import json
from frappe import _
from frappe.utils import flt
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    run_chunks_fail_fast,
    sync_records_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import (
    set_source_site_fields,
    sync_record_guard,
    temporarily_enable_transaction_items,
)
from emkan_insights.emkan_insights.uom_resolver import resolve_uom

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


# ---------------------------------------------------------------------------
# NOTE ON TRANSACTIONS
# ---------------------------------------------------------------------------

# frappe.db.rollback() anymore. They operate inside whatever transaction the
# caller (sync_rfq_docs) has open. Only the top-level loop in _sync_rfq_docs_job
# decides when a unit of work is "done" and safe to persist. This keeps each
# RFQ's sync atomic: either everything for that RFQ lands, or none of it does.
# ---------------------------------------------------------------------------


def _force_cancel_doc(doctype, docname):
    """
    INTENTIONALLY DISABLED: Force-setting docstatus via raw SQL bypasses all
    ERPNext submit/cancel hooks, leaving GL Entries, Stock Ledger Entries, and
    linked document statuses in an inconsistent state.

    This stub is kept so any remaining call sites surface a clear error instead
    of silently corrupting data. Use _safe_cancel_doc() (ORM cancel) instead.
    """
    raise frappe.ValidationError(
        f"_force_cancel_doc('{doctype}', '{docname}') is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks. "
        f"Use the ORM cancel() path."
    )


def _safe_cancel_doc(doc, log_prefix=""):
    """
    Cancel a document using the ORM (doc.cancel()), which correctly runs all
    ERPNext lifecycle hooks (GL reversal, status updates, etc.).

    If ORM cancel fails, the exception is logged as CRITICAL and re-raised.
    Raw SQL force-cancel is intentionally NOT used as a fallback — it would
    leave the ledger in an inconsistent state.
    """
    try:
        doc.flags.ignore_permissions = True
        doc.cancel()  # ORM cancel — runs all lifecycle hooks
        return True
    except Exception as cancel_err:
        frappe.log_error(
            title=f"CRITICAL: {log_prefix} ORM cancel failed — manual intervention required",
            message=(
                f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
                f"The document has NOT been cancelled. Investigate why cancel() failed "
                f"and cancel the document manually via ERPNext."
            )
        )
        raise


def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    if not remote_or_local_name:
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    for remote_field in ("custom_remote_id", "remote_id"):
        if frappe.get_meta(doctype).has_field(remote_field):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def get_child_detail(doctype, parent, item_code, remote_item_name=None):
    if not parent:
        return None

    if remote_item_name and frappe.get_meta(doctype).has_field("custom_remote_id"):
        exact = frappe.db.get_value(
            doctype,
            {"parent": parent, "custom_remote_id": remote_item_name},
            "name"
        )
        if exact:
            return exact

    return frappe.db.get_value(
        doctype,
        {"parent": parent, "item_code": item_code},
        "name"
    )


def _dedupe_rfq_children(parent_name, parenttype="Request for Quotation"):
    """
    Remove duplicate RFQ child rows (items / suppliers).
    Works for both Request for Quotation and External Request for Quotation
    - they share the same child DocTypes.

    Does NOT commit - caller controls the transaction boundary.
    """

    def _dedupe_table(doctype, parentfield, key_fields, float_fields=()):
        if not frappe.db.exists("DocType", doctype):
            return

        rows = frappe.db.get_all(
            doctype,
            filters={
                "parent": parent_name,
                "parenttype": parenttype,
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
            frappe.db.delete(doctype, {"name": ["in", delete_rows]})

    _dedupe_table(
        "Request for Quotation Item",
        "items",
        ["item_code", "warehouse", "material_request", "material_request_item", "schedule_date", "uom"],
        ["qty"],
    )

    _dedupe_table(
        "Request for Quotation Supplier",
        "suppliers",
        ["supplier", "contact", "email_id"],
    )


def _safe_dedupe(parent_name, parenttype="Request for Quotation"):
    try:
        _dedupe_rfq_children(parent_name, parenttype=parenttype)
    except Exception:
        _log(
            title=f"RFQ dedupe failed for {parenttype} {parent_name}",
            message=frappe.get_traceback(),
        )


def _sync_existing_docstatus(existing, external):
    """Handle docstatus sync for existing documents. Does NOT commit."""
    local_docstatus = frappe.db.get_value("Request for Quotation", existing, "docstatus")

    if local_docstatus == external.docstatus:
        return {"name": existing, "status": "exists"}

    try:
        rfq = frappe.get_doc("Request for Quotation", existing)
        rfq.flags.ignore_permissions = True

        if external.docstatus == 2:
            # External is cancelled - we need to cancel local too
            if local_docstatus == 0:
                # Draft locally -> submit first, then cancel
                rfq.submit()
                rfq = frappe.get_doc("Request for Quotation", existing)

            _safe_cancel_doc(rfq, log_prefix=f"RFQ {existing}")
            _safe_dedupe(rfq.name)

            return {"name": rfq.name, "status": "docstatus_synced", "docstatus": 2}

        elif external.docstatus == 1 and local_docstatus == 0:
            with temporarily_enable_transaction_items(rfq):
                rfq.submit()
            _safe_dedupe(rfq.name)

            return {"name": rfq.name, "status": "docstatus_synced", "docstatus": 1}

        else:
            _log(f"RFQ {existing} unhandled docstatus transition",
                 f"local={local_docstatus}, external={external.docstatus}")
            return {"name": existing, "status": "exists", "warning": "unhandled transition"}

    except Exception as trans_err:
        _log(f"RFQ {existing} docstatus transition failed", frappe.get_traceback())
        raise


def _sync_single_rfq(name):
    """
    Do the full sync of one External Request for Quotation -> Request for
    Quotation. Returns a result dict. Raises on unrecoverable failure so the
    caller's savepoint can roll back just this record.
    """
    # Clean duplicates on External RFQ first (items + suppliers)
    _safe_dedupe(name, parenttype="External Request for Quotation")
    frappe.clear_document_cache("External Request for Quotation", name)
    external = frappe.get_doc("External Request for Quotation", name)
    sync_started_at = external.modified

    if not external.company:
        frappe.throw(f"Company missing in External RFQ: {name}")

    # -------------------------------------------------
    # BUILD TARGET NAME
    # -------------------------------------------------
    company_abbr = frappe.db.get_value("Company", external.company, "abbr") or ""
    remote_id = external.name
    # Contract: only the External staging doctype carries the company-abbr prefix.
    # The actual RFQ uses the same name verbatim — no additional prefix is added.
    # external.name is already e.g. "CPC-RFQ-2025-00107"; adding the abbr again
    # would produce "CPC-CPC-RFQ-2025-00107".
    target_name = remote_id

    # -------------------------------------------------
    # CHECK ALREADY SYNCED
    # -------------------------------------------------
    existing = frappe.db.get_value(
        "Request for Quotation", {"custom_remote_id": remote_id}, "name"
    )
    if not existing:
        existing = frappe.db.exists("Request for Quotation", target_name)

    if existing:
        result = _sync_existing_docstatus(existing, external)

        external.db_set(
            "last_synced_on",
            sync_started_at,
            update_modified=False
        )

        return result

    # -------------------------------------------------
    # CREATE RFQ
    # -------------------------------------------------
    rfq = frappe.new_doc("Request for Quotation")

    rfq.set("items", [])
    rfq.set("suppliers", [])

    rfq.name = target_name
    rfq.flags.name_set = True

    # CRITICAL: naming_series must be set or submit fails
    rfq.naming_series = external.naming_series or "PUR-RFQ-.YYYY.-"

    rfq.company = external.company
    rfq.transaction_date = external.transaction_date
    rfq.message_for_supplier = external.message_for_supplier or "Request for Quotation"

    # CRITICAL: status must be "Draft" for insert, then submit changes it
    rfq.status = "Draft"

    # Copy all other header fields
    for field, value in external.as_dict().items():
        if (
            field not in SYSTEM_FIELDS
            and field not in {"items", "suppliers", "remote_id", "custom_remote_id",
                              "naming_series", "company", "transaction_date",
                              "message_for_supplier", "status", "docstatus",
                              "amended_from", "auto_repeat"}
            and hasattr(rfq, field)
        ):
            rfq.set(field, value)

    # CRITICAL: Clear amendment fields that block submit
    rfq.amended_from = None
    if hasattr(rfq, "auto_repeat"):
        rfq.auto_repeat = None

    rfq.custom_remote_id = remote_id
    set_source_site_fields(rfq, site_url=getattr(external, "source_site", None))

    # Resolve project
    if external.get("project"):
        rfq.project = resolve_local_doc("External Project", external.project, company_abbr)

    # -------------------------------------------------
    # MAP SUPPLIERS
    # -------------------------------------------------
    seen_suppliers = set()
    for s in external.get("suppliers", []):
        supplier_code = frappe.db.get_value("External Supplier", s.supplier, "remote_id")
        if not supplier_code:
            continue

        fingerprint = (supplier_code, s.contact)
        if fingerprint in seen_suppliers:
            continue
        seen_suppliers.add(fingerprint)

        rfq.append("suppliers", {
            "supplier": supplier_code,
            "contact": s.contact,
            "quote_status": s.quote_status or "Pending",
            "supplier_name": s.supplier_name,
            "email_id": s.email_id,
            "send_email": 0,
            "email_sent": 0,
        })

    # -------------------------------------------------
    # MAP ITEMS
    # -------------------------------------------------
    seen_items = set()
    for row in external.get("items", []):
        item_code = frappe.db.get_value("External Item", row.item_code, "remote_id")
        if not item_code:
            _log(f"RFQ item skip - no local item for {row.item_code}",
                 f"External RFQ {name}, row {row.name}")
            continue

        # Resolve warehouse
        warehouse = None
        if row.warehouse:
            warehouse = frappe.db.get_value("External Warehouse", row.warehouse, "remote_id")
        if not warehouse:
            warehouse = frappe.db.get_value(
                "Warehouse", {"is_group": 0, "company": external.company}, "name"
            )

        fingerprint = (item_code, row.qty, row.schedule_date, warehouse)
        if fingerprint in seen_items:
            continue
        seen_items.add(fingerprint)

        # Resolve Material Request with prefix
        material_request = None
        material_request_item = None
        if row.get("material_request"):
            material_request = resolve_local_doc("Material Request", row.material_request, company_abbr)
            if material_request:
                # Drop the link if the MR is cancelled — ERPNext raises
                # CancelledLinkError on submit when any linked doc is docstatus=2.
                mr_docstatus = frappe.db.get_value("Material Request", material_request, "docstatus")
                if frappe.utils.cint(mr_docstatus) == 2:
                    frappe.log_error(
                        title=f"RFQ {name}: MR link cleared (MR cancelled)",
                        message=(
                            f"Item '{item_code}': Material Request '{material_request}' "
                            f"is cancelled (docstatus=2). Clearing the link so the RFQ "
                            f"can be submitted without CancelledLinkError."
                        ),
                    )
                    material_request = None
                else:
                    material_request_item = get_child_detail(
                        "Material Request Item",
                        material_request,
                        item_code,
                        remote_item_name=row.get("material_request_item")
                    )

        rfq.append("items", {
            "item_code": item_code,
            "item_name": row.item_name,
            "description": row.description,
            "qty": row.qty,
            "uom": resolve_uom(row.uom),
            "warehouse": warehouse,
            "schedule_date": row.schedule_date,
            "supplier_part_no": row.supplier_part_no,
            "custom_remote_id": row.get("custom_remote_id") or row.name,
            "item_group": row.item_group,
            "brand": row.brand,
            "image": row.image,
            "stock_uom": resolve_uom(row.stock_uom),
            "conversion_factor": row.conversion_factor or 1,
            "stock_qty": row.stock_qty or row.qty,
            "material_request": material_request,
            "material_request_item": material_request_item,
            "project_name": row.project_name,
            "page_break": row.page_break,
        })

    if not rfq.items:
        _log(
            f"RFQ {name} skipped — no valid enabled item rows to sync",
            f"All item rows were skipped (item not found locally or disabled). "
            f"External RFQ: {name}"
        )
        return {"name": name, "status": "skipped", "reason": "no_valid_items"}

    # -------------------------------------------------
    # FLAGS
    # -------------------------------------------------
    rfq.flags.ignore_permissions = True
    rfq.flags.ignore_naming_series = True

    # -------------------------------------------------
    # INSERT — temporarily enable any disabled items so
    # Frappe's item-validation does not reject the row.
    # They are re-disabled automatically on exit.
    # -------------------------------------------------
    with temporarily_enable_transaction_items(rfq):
        rfq.insert(ignore_permissions=True)
    _safe_dedupe(rfq.name)

    # -------------------------------------------------
    # NAMING SAFETY NET
    # -------------------------------------------------
    if rfq.name != target_name:
        if not frappe.db.exists("Request for Quotation", target_name):
            old_name = rfq.name
            frappe.db.sql("""
                UPDATE `tabRequest for Quotation`
                SET name = %s
                WHERE name = %s
            """, (target_name, old_name))

            for child in ["Request for Quotation Item", "Request for Quotation Supplier"]:
                if frappe.db.exists("DocType", child):
                    frappe.db.sql(f"""
                        UPDATE `tab{child}`
                        SET parent = %s
                        WHERE parent = %s
                    """, (target_name, old_name))

            rfq.name = target_name
        else:
            _log("RFQ Sync - name collision",
                 f"Cannot rename {rfq.name} to {target_name}")

    # -------------------------------------------------
    # DOCSTATUS SYNC
    # -------------------------------------------------
    frappe.clear_document_cache("Request for Quotation", rfq.name)
    rfq = frappe.get_doc("Request for Quotation", rfq.name)

    if external.docstatus == 1:
        try:
            rfq.flags.ignore_permissions = True
            with temporarily_enable_transaction_items(rfq):
                rfq.submit()
            _safe_dedupe(rfq.name)
            result = {"name": rfq.name, "status": "synced", "docstatus": 1}

        except Exception as submit_err:
            tb = frappe.get_traceback()
            _log(f"RFQ {rfq.name} SUBMIT FAILED",
                 f"Error: {str(submit_err)}\n\nTraceback:\n{tb}")
            # Re-raise so sync_record_guard rolls back the draft insert and
            # the job loop records this as a genuine failure.
            raise

    elif external.docstatus == 2:
        try:
            rfq.flags.ignore_permissions = True

            # Submit first (required before ORM cancel)
            with temporarily_enable_transaction_items(rfq):
                rfq.submit()

            rfq = frappe.get_doc("Request for Quotation", rfq.name)
            with temporarily_enable_transaction_items(rfq):
                _safe_cancel_doc(rfq, log_prefix=f"RFQ {rfq.name}")
            _safe_dedupe(rfq.name)

            final_docstatus = frappe.db.get_value("Request for Quotation", rfq.name, "docstatus")

            if final_docstatus == 2:
                result = {"name": rfq.name, "status": "synced", "docstatus": 2}
            else:
                _log(f"RFQ {rfq.name} CANCEL VERIFICATION FAILED",
                     f"Expected docstatus=2, got docstatus={final_docstatus}")
                raise Exception(
                    f"RFQ {rfq.name}: cancel verification failed - "
                    f"docstatus is {final_docstatus}"
                )

        except Exception as cancel_err:
            # ORM cancel failed. Re-raise so sync_record_guard rolls back the
            # partial write and the job loop counts this record as failed.
            # _sync_existing_docstatus will retry the cancel on the next run.
            _log(
                f"RFQ {rfq.name} CANCEL FAILED — CRITICAL, manual intervention may be required",
                f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
                f"The RFQ is stuck at docstatus=1. Cancel manually via ERPNext or "
                f"investigate the validation error blocking cancel()."
            )
            raise

    else:
        # docstatus = 0 (Draft) - already in correct state
        result = {"name": rfq.name, "status": "synced", "docstatus": 0}

    external.db_set("remote_id", rfq.name, update_modified=False)
    external.db_set(
        "last_synced_on",
        sync_started_at,
        update_modified=False
    )
    return result


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_rfq_docs(source_doctype: str, names, sync_now=False):
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

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: _sync_rfq_docs_job(names=chunk),
        )

    job_name = f"rfq_fetch_chunk_{frappe.generate_hash(length=8)}_0"
    frappe.enqueue(
        "emkan_insights.emkan_insights.external_rfq_sync._sync_rfq_docs_job",
        queue="long",
        job_name=job_name,
        timeout=1000,
        names=unique_names,
        batch_index=0,
    )

    return (
        f"Sync Queued: {len(unique_names)} record(s) in sequential "
        f"batches of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def _sync_rfq_docs_job(names, batch_index=0):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    batch_index = int(batch_index or 0)
    chunk = names[batch_index * CHUNK_SIZE:(batch_index + 1) * CHUNK_SIZE]

    results = sync_records_fail_fast(
        chunk,
        _sync_single_rfq,
        source_doctype="External Request for Quotation",
        record_context=lambda name: sync_record_guard("External Request for Quotation", name),
    )

    next_batch_index = batch_index + 1
    if next_batch_index * CHUNK_SIZE < len(names):
        job_name = f"rfq_sync_chunk_{frappe.generate_hash(length=8)}_{next_batch_index}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_rfq_sync._sync_rfq_docs_job",
            queue="long",
            job_name=job_name,
            timeout=1000,
            names=names,
            batch_index=next_batch_index,
        )

    return results
