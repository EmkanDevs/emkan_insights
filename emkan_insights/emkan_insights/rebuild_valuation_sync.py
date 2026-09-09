"""
Full valuation rebuild orchestrator.

The remote site switched to Moving Average; local copies were synced under a
broken rate pipeline and sit on a ledger whose inbound stock carries no value.
This tool rebuilds local transactions from the External staging records:

    1. valuation_coverage()          read-only report of unvalued stock
    2. wipe(dry_run=True)            ORM-cancel + delete sync-created docs,
                                     downstream first
    3. reset_markers()               clear last_synced_on / is_synced so every
                                     External record becomes pending again
    4. resync(doctype)               sequentially re-run each module's sync job
                                     in dependency order

Run steps in this order. Nothing destructive happens while dry_run=True.
"""

import json

import frappe
from frappe.utils import cint, flt

from emkan_insights.emkan_insights.sync_guard import (
    temporarily_enable_transaction_items,
)


def _report_progress(doctype, done, total):
    frappe.cache().set_value(
        "rebuild_progress",
        json.dumps({
            "phase": "wipe",
            "doctype": doctype,
            "done": done,
            "total": total,
        }),
    )

# ==========================================================
# IDENTITY FIELDS marking a document as sync-created
# ==========================================================

SYNC_MARKER_FIELDS = (
    "remote_id",
    "custom_remote_id",
    "source_site",
    "custom_source_site",
)

EXTERNAL_DOCTYPE = {
    "Journal Entry": "External Journal Entry",
    "Payment Entry": "External Payment Entry",
    "Sales Invoice": "External Sales Invoice",
    "Delivery Note": "External Delivery Note",
    "Purchase Invoice": "External Purchase Invoice",
    "Stock Entry": "External Stock Entry",
    "Purchase Receipt": "External Purchase Receipt",
    "Sales Order": "External Sales Order",
    "Supplier Quotation": "External Supplier Quotation",
    "Purchase Order": "External Purchase Order",
    "Quotation": "External Quotation",
    "Request for Quotation": "External Request for Quotation",
    "Material Request": "External Material Request",
    "Stock Reconciliation": "External Stock Reconciliation",
}

# Downstream first, so cancels are never blocked by a submitted dependent.
WIPE_ORDER = [
    "Journal Entry",
    "Payment Entry",
    "Sales Invoice",
    "Delivery Note",
    "Purchase Invoice",
    "Stock Entry",
    "Purchase Receipt",
    "Sales Order",
    "Supplier Quotation",
    "Purchase Order",
    "Quotation",
    "Request for Quotation",
    "Material Request",
    "Stock Reconciliation",
]

# Upstream first, so valued inbound posts before issues are priced.
RESYNC_ORDER = [
    "Material Request",
    "Purchase Order",
    "Purchase Receipt",
    "Stock Entry",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
]


# ==========================================================
# STEP 0: COVERAGE REPORT (read-only)
# ==========================================================

def valuation_coverage():
    """
    Item/warehouse combos holding quantity without valued inbound stock —
    exactly what a backdated Stock Reconciliation must cover before issues
    can post real values.
    """
    bins = frappe.get_all(
        "Bin",
        filters={"actual_qty": ["!=", 0]},
        fields=["item_code", "warehouse", "actual_qty", "stock_value"],
    )

    unvalued = [b for b in bins if b.actual_qty > 0 and flt(b.stock_value) == 0]
    negative = [b for b in bins if b.actual_qty < 0]

    def summarize(rows, label):
        print(f"\n{label}: {len(rows)} bin(s)")
        by_wh = {}
        for b in rows:
            by_wh.setdefault(b.warehouse, []).append(b)
        for wh in sorted(by_wh, key=lambda w: -len(by_wh[w]))[:15]:
            items = ", ".join(sorted(x.item_code for x in by_wh[wh])[:8])
            more = "" if len(by_wh[wh]) <= 8 else f" (+{len(by_wh[wh]) - 8} more)"
            print(f"  {wh}: {len(by_wh[wh])} item(s) [{items}{more}]")

    print(f"Bins with stock: {len(bins)}")
    summarize(unvalued, "Positive qty but ZERO value (needs backdated SR)")
    summarize(negative, "NEGATIVE qty (issues without any inbound)")

    return {"unvalued": unvalued, "negative": negative}


def _wipe_error_sample(limit=8):
    """Group today's rebuild wipe errors by their first traceback line."""
    rows = frappe.get_all(
        "Error Log",
        filters={
            "creation": [">", frappe.utils.nowdate()],
            "method": ["like", "%Rebuild wipe failed%"],
        },
        fields=["name", "error"],
        order_by="creation desc",
        limit_page_length=400,
    )
    groups = {}
    for r in rows:
        lines = [ln.strip() for ln in (r.error or "").splitlines() if ln.strip()]
        key = next(
            (ln for ln in lines if ln.startswith(("<", "frappe.", "_mysql", "pymysql", "ValueError", "ValidationError")))
            or (lines[-1] if lines else "unknown"),
            "unknown",
        )[:120]
        groups[key] = groups.get(key, 0) + 1
    return dict(sorted(groups.items(), key=lambda kv: -kv[1])[:limit])


def site_company_audit():
    """
    Report which companies/sites the remaining sync-created documents belong
    to, per doctype — confirms whether the wipe scope matches intent.
    """
    MARKERS = ("remote_id", "custom_remote_id", "source_site", "custom_source_site")
    out = {}
    try:
        configs = frappe.get_all(
            "External Site Configuration", fields=["name", "company", "site_url"]
        )
        out["configs"] = [dict(r) for r in configs]
    except Exception as e:
        out["configs"] = str(e)

    for dt in WIPE_ORDER:
        cols = {
            r[0]
            for r in frappe.db.sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                ("tab" + dt,),
            )
        }
        conds = ["`{0}` IS NOT NULL AND `{0}` != ''".format(c) for c in MARKERS if c in cols]
        if not conds:
            continue
        rows = frappe.db.sql(
            "SELECT company, count(*) c FROM `tab%s` WHERE %s GROUP BY company"
            % (dt, " OR ".join(conds))
        )
        out[dt] = {r[0]: int(r[1]) for r in rows}
    return out


# ==========================================================
# STEP 1: WIPE SYNC-CREATED DOCUMENTS
# ==========================================================

def _marker_condition(meta):
    present = [f for f in SYNC_MARKER_FIELDS if meta.has_field(f)]
    if not present:
        return None
    return " OR ".join(f"`{f}` IS NOT NULL AND `{f}` != ''" for f in present)


def synced_doc_names(doctype, company=None):
    """Names of sync-created documents, optionally restricted to one company."""
    meta = frappe.get_meta(doctype)
    cond = _marker_condition(meta)
    if not cond:
        return []
    query = f"SELECT name FROM `tab{doctype}` WHERE ({cond})"
    params = ()
    if company:
        query += " AND company = %s"
        params = (company,)
    return frappe.db.sql(query, params, pluck=True)


def _delete_one(doctype, name):
    doc = frappe.get_doc(doctype, name)
    with temporarily_enable_transaction_items(doc):
        doc.flags.ignore_permissions = True
        if doc.docstatus == 1:
            doc.cancel()
        frappe.delete_doc(doctype, name, force=True)


def wipe(doctype, dry_run=True, company=None):
    """Cancel + delete every sync-created document of one doctype."""
    if doctype not in EXTERNAL_DOCTYPE:
        raise ValueError(f"{doctype} is not a rebuildable doctype")

    names = synced_doc_names(doctype, company=company)
    scope = f" [{company}]" if company else ""
    print(f"{doctype}{scope}: {len(names)} sync-created document(s)")
    if dry_run or not names:
        return {"doctype": doctype, "count": len(names), "deleted": 0}

    done, failed = 0, 0
    for i, name in enumerate(names, start=1):
        try:
            _delete_one(doctype, name)
            done += 1
        except Exception:
            failed += 1
            frappe.log_error(
                title=f"Rebuild wipe failed: {doctype} {name}",
                message=frappe.get_traceback(),
            )
            frappe.db.rollback()
        if i % 200 == 0:
            _report_progress(doctype, i, len(names))
            print(f"  ...{i}/{len(names)}")
    _report_progress(doctype, len(names), len(names))

    print(f"{doctype}{scope}: deleted {done}, failed {failed} (see Error Log)")
    return {"doctype": doctype, "count": len(names), "deleted": done, "failed": failed}


def wipe_all(dry_run=True, doctypes=None, company=None):
    results = []
    for dt in (doctypes or WIPE_ORDER):
        results.append(wipe(dt, dry_run=dry_run, company=company))
    total = sum(r["count"] for r in results)
    print(f"\n{'DRY RUN — nothing deleted' if dry_run else 'WIPE COMPLETE'}: "
          f"{total} document(s) targeted across {len(results)} doctype(s)")
    return results


def run_rebuild_wipe(dry_run=False, company=None, max_passes=3):
    """
    Worker entry point: wipe passes until counts stabilize, then marker
    reset. Later passes pick up docs whose cancel was blocked by
    dependents deleted in earlier passes.
    """
    all_results = []
    for pass_no in range(1, max_passes + 1):
        frappe.cache().set_value("rebuild_progress", json.dumps({
            "phase": f"wipe pass {pass_no}", "doctype": "-", "done": 0, "total": 0,
        }))
        results = wipe_all(dry_run=dry_run, company=company)
        all_results.append(results)
        targeted = sum(r["count"] for r in results)
        if not targeted:
            break

    reset_markers(dry_run=False, company=company)
    frappe.cache().set_value("rebuild_progress", json.dumps({
        "phase": "wipe", "doctype": "DONE", "done": 1, "total": 1,
    }))
    frappe.log_error(
        title="REBUILD WIPE COMPLETE",
        message=frappe.as_json(all_results),
    )


# ==========================================================
# STEP 2: RESET EXTERNAL SYNC MARKERS
# ==========================================================

def reset_markers(dry_run=True, doctypes=None, company=None):
    """Clear last_synced_on / is_synced so matching External records re-sync."""
    doctypes = doctypes or list(EXTERNAL_DOCTYPE.values())
    for ext_dt in doctypes:
        if not frappe.db.exists("DocType", ext_dt):
            continue
        meta = frappe.get_meta(ext_dt)
        updates = {}
        for field in ("last_synced_on", "is_synced"):
            if meta.has_field(field):
                updates[field] = None
        if not updates:
            continue

        where = "1=1"
        params = []
        if company and meta.has_field("company"):
            where = "company = %s"
            params.append(company)

        count = frappe.db.sql(
            f"SELECT count(*) c FROM `tab{ext_dt}` WHERE {where}", tuple(params), as_dict=True
        )[0].c
        print(f"{ext_dt}: resetting markers on {count} record(s)")
        if dry_run:
            continue
        set_clause = ", ".join(f"`{f}` = %s" for f in updates)
        frappe.db.sql(
            f"UPDATE `tab{ext_dt}` SET {set_clause} WHERE {where}",
            tuple(updates.values()) + tuple(params),
        )
    if not dry_run:
        print("Markers reset.")


# ==========================================================
# STEP 3: SEQUENTIAL RESYNC (upstream first)
# ==========================================================

def _pending_names(ext_doctype, batch_size=500, company=None):
    where = "last_synced_on IS NULL OR modified > last_synced_on"
    params = []
    if company:
        meta = frappe.get_meta(ext_doctype)
        if meta.has_field("company"):
            where += " AND company = %s"
            params.append(company)
        else:
            frappe.logger("rebuild").warning(
                "%s has no company field; company filter ignored", ext_doctype
            )
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{ext_doctype}` WHERE {where} ORDER BY creation",
        tuple(params),
        pluck=True,
    )
    return list(rows)[:batch_size]


def resync(doctype, batch_size=500, dry_run=False, company=None):
    """
    Run one doctype's sync job synchronously over pending External records.
    Call repeatedly (or raise batch_size) until 'pending' reaches zero.
    """
    jobs = {
        "Material Request": lambda n: _imp("external_material_request_sync", "sync_bulk_material_requests")(n),
        "Purchase Order": lambda n: _imp("external_po_sync", "sync_bulk_purchase_orders")(n),
        "Purchase Receipt": lambda n: _imp("external_pr_sync", "sync_bulk_purchase_receipts")(n),
        "Stock Entry": lambda n: _imp("external_stock_entry_sync", "_sync_stock_entry_docs_job")(
            source_doctype="External Stock Entry", names=n, batch_index=0
        ),
        "Purchase Invoice": lambda n: _imp("external_pi_sync", "_sync_purchase_invoice_docs_job")(
            source_doctype="External Purchase Invoice", names=n
        ),
        "Payment Entry": lambda n: _imp("external_payment_entry_sync", "_sync_payment_entry_docs_job")(
            source_doctype="External Payment Entry", names=n
        ),
        "Journal Entry": lambda n: _imp("external_journal_entry_sync", "_sync_journal_entries_job")(n),
    }

    if doctype not in jobs:
        raise ValueError(f"No inline sync job wired for {doctype}")

    ext_dt = EXTERNAL_DOCTYPE[doctype]
    names = _pending_names(ext_dt, batch_size, company=company)
    scope = f" [{company}]" if company else ""
    print(f"{doctype}{scope}: syncing {len(names)} pending record(s)")

    if dry_run or not names:
        return []

    result = jobs[doctype](names)

    remaining = frappe.db.count(
        ext_dt, {"last_synced_on": ("is", "not set")}
    ) if frappe.get_meta(ext_dt).has_field("last_synced_on") else "?"
    print(f"{doctype}: batch done, {remaining} still unsynced")
    return result


def _imp(module, func):
    import importlib
    m = importlib.import_module(f"emkan_insights.emkan_insights.{module}")
    return getattr(m, func)


def resync_all(batch_size=500, dry_run=False, company=None):
    """Run one upstream-first pass over every rebuildable doctype."""
    for dt in RESYNC_ORDER:
        try:
            resync(dt, batch_size=batch_size, dry_run=dry_run, company=company)
        except Exception:
            frappe.log_error(
                title=f"Rebuild resync failed: {dt}",
                message=frappe.get_traceback(),
            )
            frappe.db.rollback()
