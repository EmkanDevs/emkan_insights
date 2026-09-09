import frappe
import json
from frappe.utils import cint, flt
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
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

DEFAULT_WAREHOUSE = "Stores - IMC"

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


def _resolve_usable_warehouse(warehouse, company):
    if warehouse:
        details = frappe.db.get_value("Warehouse", warehouse, ["disabled", "is_group", "company"], as_dict=True)
        if (
            details
            and not details.disabled
            and not details.is_group
            and (not company or details.company == company)
        ):
            return warehouse

    return frappe.db.get_value(
        "Warehouse",
        {"company": company, "disabled": 0, "is_group": 0},
        "name",
    )


def _resolve_quotation(remote_qtn_name, company_abbr=None):
    
    if not remote_qtn_name:
        return None

    remote_qtn_name = str(remote_qtn_name).strip()
    if not remote_qtn_name:
        return None

    candidates = [remote_qtn_name]
    
    if company_abbr and not remote_qtn_name.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_qtn_name}")

    seen = set()
    unique = []
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)

    for name in unique:
        if frappe.db.exists("Quotation", name):
            return name

    meta = frappe.get_meta("Quotation")
    for name in unique:
        for field in ("remote_id", "custom_remote_id"):
            if meta.has_field(field):
                found = frappe.db.get_value("Quotation", {field: name}, "name")
                if found:
                    return found

    for name in unique:
        if frappe.db.exists("External Quotation", name):
            linked = frappe.db.get_value("External Quotation", name, "remote_id")
            if linked and frappe.db.exists("Quotation", linked):
                return linked

    return None


def _resolve_quotation_item(quotation_name, remote_item_id=None, item_code=None):
    """Map remote quotation_item id → local Quotation Item name."""
    if not quotation_name:
        return None

    if remote_item_id and frappe.get_meta("Quotation Item").has_field("custom_remote_id"):
        found = frappe.db.get_value(
            "Quotation Item",
            {"parent": quotation_name, "custom_remote_id": remote_item_id},
            "name",
        )
        if found:
            return found

    if item_code:
        return frappe.db.get_value(
            "Quotation Item",
            {"parent": quotation_name, "item_code": item_code},
            "name",
        )

    return None


def _resolve_amended_from(remote_amended_from, company_abbr=None):
    
    if not remote_amended_from:
        return None

    remote_amended_from = str(remote_amended_from).strip()
    if not remote_amended_from:
        return None

    candidates = [remote_amended_from]
    if company_abbr and not remote_amended_from.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_amended_from}")

    local_name = None
    for name in candidates:
        if frappe.db.exists("Sales Order", name):
            local_name = name
            break
        found = frappe.db.get_value("Sales Order", {"remote_id": name}, "name")
        if found:
            local_name = found
            break

    if not local_name:
        return None

    if cint(frappe.db.get_value("Sales Order", local_name, "docstatus")) != 2:
        return None

    return local_name


def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message or "")


def _set_docstatus_sql(doctype, docname, docstatus, status):
    """
    INTENTIONALLY DISABLED: Force-setting docstatus via raw SQL bypasses all
    ERPNext submit/cancel hooks. This leaves GL Entries, linked document
    statuses, and reserved quantities in an inconsistent state.

    This stub is kept so any remaining call sites surface a clear error
    instead of silently corrupting data. Use the ORM submit()/cancel() path.
    """
    raise frappe.ValidationError(
        f"_set_docstatus_sql('{doctype}', '{docname}', {docstatus}) is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks."
    )


def _force_cancel_doc(doctype, docname):
    """
    INTENTIONALLY DISABLED. See _set_docstatus_sql for rationale.
    """
    raise frappe.ValidationError(
        f"_force_cancel_doc('{doctype}', '{docname}') is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks."
    )


def _force_submit_doc(doctype, docname):
    """
    INTENTIONALLY DISABLED. See _set_docstatus_sql for rationale.
    """
    raise frappe.ValidationError(
        f"_force_submit_doc('{doctype}', '{docname}') is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks."
    )


def _safe_cancel_doc(doc, log_prefix=""):
    """
    Cancel a document using the ORM (doc.cancel()), which correctly runs all
    ERPNext lifecycle hooks. If ORM cancel fails, the exception is logged as
    CRITICAL and re-raised. Raw SQL force-cancel is NOT used as a fallback.
    """
    try:
        doc.flags.ignore_permissions = True
        with temporarily_enable_transaction_items(doc):
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


def _safe_submit_doc(doc, log_prefix=""):
    """
    Submit a document using the ORM (doc.submit()), which correctly runs all
    ERPNext lifecycle hooks. If ORM submit fails, the exception is logged as
    CRITICAL and re-raised. Raw SQL force-submit is NOT used as a fallback.
    """
    try:
        doc.flags.ignore_permissions = True
        with temporarily_enable_transaction_items(doc):
            doc.submit()  # ORM submit — runs all lifecycle hooks
        return True
    except Exception as submit_err:
        # submit() may have already written docstatus=1 before on_submit threw.
        # If so, treat as succeeded (same guard as before).
        current = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
        if current == 1:
            return True

        frappe.log_error(
            title=f"CRITICAL: {log_prefix} ORM submit failed — manual intervention required",
            message=(
                f"Error: {str(submit_err)}\n\n{frappe.get_traceback()}\n\n"
                f"The document has NOT been submitted. Investigate why submit() failed."
            )
        )
        raise


def _sync_existing_docstatus(existing_name, external):
    """
    Sync docstatus for an already-existing Sales Order.
    Fixes submitted External SOs that were left as local Draft because
    re-sync previously returned status=exists without updating docstatus.
    Same pattern as external_stock_entry_sync._sync_existing_docstatus.
    """
    local_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
    target_docstatus = cint(external.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists", "docstatus": local_docstatus}

    try:
        so = frappe.get_doc("Sales Order", existing_name)

        if target_docstatus == 2:
            if local_docstatus == 0:
                _safe_submit_doc(so, log_prefix=f"Sales Order {existing_name}")
                so.reload()

            _safe_cancel_doc(so, log_prefix=f"Sales Order {existing_name}")
            final_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
            if final_docstatus == 2:
                return {"name": existing_name, "status": "docstatus_synced", "docstatus": 2}
            raise Exception(
                f"Sales Order {existing_name}: cancel verification failed - docstatus={final_docstatus}"
            )

        if target_docstatus == 1 and local_docstatus == 0:
            _safe_submit_doc(so, log_prefix=f"Sales Order {existing_name}")
            final_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
            if final_docstatus == 1:
                return {"name": existing_name, "status": "docstatus_synced", "docstatus": 1}
            raise Exception(
                f"Sales Order {existing_name}: submit verification failed - docstatus={final_docstatus}"
            )

        _log(
            f"Sales Order {existing_name} unhandled docstatus transition",
            f"local={local_docstatus}, external={target_docstatus}",
        )
        return {
            "name": existing_name,
            "status": "exists",
            "warning": "unhandled transition",
            "docstatus": local_docstatus,
        }

    except Exception as trans_err:
        # If a prior partial submit already landed, treat as synced
        final_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
        if final_docstatus == target_docstatus:
            return {"name": existing_name, "status": "docstatus_synced", "docstatus": final_docstatus}

        _log(f"Sales Order {existing_name} docstatus transition failed", frappe.get_traceback())
        raise


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_sales_order_docs(source_doctype, names, sync_now=False):
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
        # Sequential pipeline path: strict fail-fast. Submit/cancel failures
        # are fatal here (no synced_draft downgrades), and chunks run in order.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: _sync_sales_order_docs_job(
                source_doctype=source_doctype,
                names=chunk,
                strict=True,
            ),
        )

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"so_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_sales_order_sync._sync_sales_order_docs_job",
            queue="long",
            job_name=job_name,
            timeout=1000,
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

def _sync_sales_order_docs_job(source_doctype, names, strict=False):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            with sync_record_guard(source_doctype, name):
                ext_so = frappe.get_doc(source_doctype, name)
            sync_started_at = ext_so.modified
            remote_id = ext_so.name

            # ------------------------------------------------
            # BUILD TARGET NAME: {company_abbr}-{remote_id}
            # Same pattern as Work Order sync
            # ------------------------------------------------
            company_abbr = frappe.db.get_value('Company', ext_so.company, 'abbr') or ''
            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # CHECK EXISTING (by remote_id, same as Work Order)
            # ------------------------------------------------
            existing_so = frappe.db.get_value(
                "Sales Order",
                {"remote_id": remote_id},
                "name"
            ) or (target_name if frappe.db.exists("Sales Order", target_name) else None)

            if existing_so:
                # Same as Stock Entry: still sync docstatus on re-sync
                existing_result = _sync_existing_docstatus(existing_so, ext_so)
                results.append(existing_result)
                ext_so.db_set(
                    "last_synced_on",
                    sync_started_at,
                    update_modified=False
                )
                continue

            so = frappe.new_doc("Sales Order")

            # ------------------------------------------------
            # SET NAME WITH COMPANY ABBR PREFIX
            # Same pattern as Work Order
            # ------------------------------------------------
            so.remote_id = remote_id
            so.name = target_name
            so.flags.name_set = True

            src_site_value = getattr(ext_so, "site_url", None) or getattr(ext_so, "source_site", None)
            if src_site_value:
                set_source_site_fields(so, site_url=src_site_value)
                if not any(so.meta.has_field(fieldname) for fieldname in ("source_site", "site_url", "custom_source_site")):
                    frappe.log_error(
                        f"SO Sync: source_site value '{src_site_value}' not mapped — "
                        f"Sales Order missing 'source_site'/'site_url' Custom Field.",
                        "SO Sync: missing source_site field"
                    )

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            so.flags.ignore_permissions = True
            so.flags.ignore_naming_series = True

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # NOTE: All child-table fields (items, taxes,
            # payment_schedule, etc.) must be excluded here and
            # rebuilt explicitly below. Otherwise Frappe tries to
            # reuse the source doc's child row names, which
            # already exist in the DB -> DuplicateEntryError.
            # ------------------------------------------------
            for field, value in ext_so.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in [
                        "items", "taxes", "payment_schedule",
                        "remote_id", "amended_from",
                    ]
                    and hasattr(so, field)
                ):
                    so.set(field, value)

            # Resolve amended_from to local cancelled SO (prefix / remote_id).
            # Blind copy of remote amended_from fails validate_amended_from.
            so.amended_from = _resolve_amended_from(
                getattr(ext_so, "amended_from", None), company_abbr
            )

            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
            so.set("items", [])
            conversion_rate = flt(getattr(ext_so, "conversion_rate", 0)) or 1.0

            for row in ext_so.items:
                item_code = row.get("item_code")
                local_item_code = f"{company_abbr}-{item_code}" if company_abbr and item_code else item_code
                if local_item_code and frappe.db.exists("Item", local_item_code):
                    item_code = local_item_code

                if not frappe.db.exists("Item", item_code):
                    prefixed_item_group = f"{company_abbr}-All Item Groups" if company_abbr else None
                    frappe.get_doc({
                        "doctype": "Item",
                        "item_code": item_code,
                        "item_name": row.get("item_name") or item_code,
                        "item_group": (
                            prefixed_item_group
                            if prefixed_item_group and frappe.db.exists("Item Group", prefixed_item_group)
                            else "All Item Groups"
                        ),
                        "stock_uom": resolve_uom(row.get("uom") or "Nos")
                    }).insert(ignore_permissions=True)

                item_name = (
                    frappe.db.get_value("Item", item_code, "item_name")
                    or row.get("item_name")
                    or item_code
                )

                qty    = flt(row.get("qty")) or 1
                rate   = flt(row.get("rate")) or 0
                amount = flt(row.get("amount")) if flt(row.get("amount")) else qty * rate

                # prevdoc_docname = remote Quotation (e.g. SAL-QTN-…);
                # resolve to local prefixed Quotation (e.g. IMC-SAL-QTN-…)
                prevdoc_docname = _resolve_quotation(
                    row.get("prevdoc_docname"), company_abbr
                )
                quotation_item = None
                if prevdoc_docname:
                    quotation_item = _resolve_quotation_item(
                        prevdoc_docname,
                        remote_item_id=row.get("quotation_item"),
                        item_code=item_code,
                    )

                so.append("items", {
                    "item_code":            item_code,
                    "item_name":            item_name,
                    "description":          row.get("description") or item_name,
                    "custom_remote_id":     row.get("custom_remote_id"),  
                    "qty":                  qty,
                    "stock_qty":            qty,
                    "rate":                 rate,
                    "amount":               amount,
                    "net_rate":             flt(row.get("net_rate")) or rate,
                    "net_amount":           flt(row.get("net_amount")) or amount,
                    "base_rate":            rate * conversion_rate,
                    "base_amount":          amount * conversion_rate,
                    "base_net_rate":        (flt(row.get("net_rate")) or rate) * conversion_rate,
                    "base_net_amount":      (flt(row.get("net_amount")) or amount) * conversion_rate,
                    "price_list_rate":      flt(row.get("price_list_rate")),
                    "base_price_list_rate": flt(row.get("base_price_list_rate")),
                    "discount_percentage":  flt(row.get("discount_percentage")),
                    "discount_amount":      flt(row.get("discount_amount")),
                    "item_tax_rate":        row.get("item_tax_rate") or "{}",
                    "uom":                  resolve_uom(row.get("uom") or "Nos"),
                    "stock_uom":            resolve_uom(row.get("stock_uom") or row.get("uom") or "Nos"),
                    "conversion_factor":    row.get("conversion_factor") or 1,
                    "warehouse":            _resolve_usable_warehouse(row.get("warehouse"), ext_so.company) or DEFAULT_WAREHOUSE,
                    "delivery_date":        row.get("delivery_date"),
                    "cost_center":          row.get("cost_center"),
                    "project":              row.get("project") or getattr(ext_so, "project", None),
                    "prevdoc_docname":      prevdoc_docname,
                    "quotation_item":       quotation_item,
                })

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_so, "taxes"):
                so.set("taxes", [])
                for row in ext_so.taxes:
                    so.append("taxes", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })

            # ------------------------------------------------
            # PAYMENT SCHEDULE
            # Rebuilt explicitly (like taxes) so that stale child
            # row names from the source doc are stripped out and
            # Frappe generates fresh names for the new doc.
            # ------------------------------------------------
            if hasattr(ext_so, "payment_schedule"):
                so.set("payment_schedule", [])
                for row in ext_so.payment_schedule:
                    so.append("payment_schedule", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })
            # ------------------------------------------------
            so.total                = flt(getattr(ext_so, "total", 0))
            so.net_total             = flt(getattr(ext_so, "net_total", 0)) or so.total
            so.base_total            = flt(getattr(ext_so, "base_total", 0))
            so.base_net_total        = flt(getattr(ext_so, "base_net_total", 0)) or so.base_total
            so.total_qty             = flt(getattr(ext_so, "total_qty", 0))
            so.grand_total           = flt(getattr(ext_so, "grand_total", 0))
            so.base_grand_total      = flt(getattr(ext_so, "base_grand_total", 0))
            so.total_taxes_and_charges      = flt(getattr(ext_so, "total_taxes_and_charges", 0))
            so.base_total_taxes_and_charges = flt(getattr(ext_so, "base_total_taxes_and_charges", 0))
            so.rounding_adjustment          = flt(getattr(ext_so, "rounding_adjustment", 0))
            so.base_rounding_adjustment      = flt(getattr(ext_so, "base_rounding_adjustment", 0))
            so.rounded_total                 = flt(getattr(ext_so, "rounded_total", 0))
            so.base_rounded_total            = flt(getattr(ext_so, "base_rounded_total", 0))
            so.in_words                      = getattr(ext_so, "in_words", "")
            so.base_in_words                 = getattr(ext_so, "base_in_words", "")
            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            with temporarily_enable_transaction_items(so):
                so.insert(
                    ignore_permissions=True,
                )

            # ------------------------------------------------
            # NAMING SAFETY NET: If Frappe renamed it
            # Same pattern as Work Order
            # ------------------------------------------------
            if so.name != target_name:

                if not frappe.db.exists("Sales Order", target_name):
                    frappe.db.sql("""
                        UPDATE `tabSales Order`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, so.name))

                old_name = so.name

                child_tables = [
                    "Sales Order Item",
                    "Sales Order Tax",
                    "Payment Schedule",
                ]

                for child_table in child_tables:
                    if frappe.db.exists("DocType", child_table):
                        frappe.db.sql("""
                            UPDATE `tab{0}`
                            SET parent = %s
                            WHERE parent = %s
                        """.format(child_table), (target_name, old_name))

                so.name = target_name

            # ------------------------------------------------
            # VERIFY: confirm items actually landed
            # ------------------------------------------------
            actual_item_count = frappe.db.count("Sales Order Item", {"parent": target_name})
            expected_item_count = len(ext_so.get("items") or [])

            if actual_item_count != expected_item_count:
                frappe.log_error(
                    f"Sales Order {target_name}: expected {expected_item_count} "
                    f"items, found {actual_item_count} after insert.",
                    "ExternalSalesOrder Sync - items mismatch"
                )

            # ------------------------------------------------
            # DOCSTATUS SYNC (same pattern as Stock Entry sync)
            # ------------------------------------------------
            target_status = cint(ext_so.docstatus)
            frappe.clear_document_cache("Sales Order", target_name)
            updated_so = frappe.get_doc("Sales Order", target_name)

            if target_status == 1:
                try:
                    _safe_submit_doc(updated_so, log_prefix=f"Sales Order {target_name}")
                    results.append({
                        "name": target_name,
                        "status": "synced",
                        "docstatus": 1,
                        "items_count": actual_item_count,
                    })
                except Exception as submit_err:
                    _log(f"Sales Order {target_name} SUBMIT FAILED",
                         frappe.get_traceback())
                    if strict:
                        # Pipeline mode: a failed submit is a real failure —
                        # re-raise so the record stays retryable.
                        raise
                    results.append({
                        "name": target_name,
                        "status": "synced_draft",
                        "docstatus": cint(frappe.db.get_value("Sales Order", target_name, "docstatus")),
                        "error": f"Submit failed: {str(submit_err)}",
                        "items_count": actual_item_count,
                    })

            elif target_status == 2:
                try:
                    _safe_submit_doc(updated_so, log_prefix=f"Sales Order {target_name}")
                    updated_so = frappe.get_doc("Sales Order", target_name)
                    _safe_cancel_doc(updated_so, log_prefix=f"Sales Order {target_name}")

                    final_status = cint(frappe.db.get_value("Sales Order", target_name, "docstatus"))
                    results.append({
                        "name": target_name,
                        "status": "synced" if final_status == 2 else "sync_error",
                        "docstatus": final_status,
                        "items_count": actual_item_count,
                    })
                    if strict and final_status != 2:
                        raise Exception(
                            f"Sales Order {target_name}: cancel verification failed - "
                            f"docstatus is {final_status}"
                        )
                except Exception as cancel_err:
                    # ORM submit/cancel failed. Do NOT fall back to raw SQL.
                    # Leave the SO at its current docstatus; _sync_existing_docstatus
                    # will retry on the next sync run.
                    _log(
                        f"Sales Order {target_name} CANCEL FAILED — CRITICAL",
                        f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
                        f"The SO may be stuck at docstatus=1. Cancel manually via ERPNext."
                    )
                    if strict:
                        raise
                    results.append({
                        "name": target_name,
                        "status": "synced_submitted",
                        "error": f"Cancel failed: {str(cancel_err)}",
                        "items_count": actual_item_count,
                    })
            else:
                results.append({
                    "name": target_name,
                    "status": "synced",
                    "docstatus": 0,
                    "items_count": actual_item_count,
                })

            ext_so.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

        except Exception as e:
            frappe.db.rollback()

            log_record_failure("Sales Order", name, str(e))

            if strict:
                raise

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })
            break

    return results
