"""
Repair Stock Entries synced with zero/incorrect valuation rates.

For each External Stock Entry that has a local counterpart whose item rates
diverge from the remote record:
  1. ORM-cancel the local Stock Entry (reverses its SLEs and GL entries).
  2. Delete the cancelled document so the sync recreates it from scratch.
  3. Re-run sync_external_stock_entry under the fixed code, which copies the
     authoritative remote rates into the reposted stock movement.

Usage (from bench):
    bench --site <site> execute repair_stock_entry_rates.analyze --kwargs "{'limit': 20}"
    bench --site <site> execute repair_stock_entry_rates.repair_one --args "['CPC-MAT-STE-2026-02952']"
    bench --site <site> execute repair_stock_entry_rates.repair_all --kwargs "{'limit': 50}"
"""

import frappe
from frappe.utils import cint, flt

from emkan_insights.emkan_insights.sync_guard import (
    temporarily_enable_transaction_items,
)

RATE_FIELDS = ("basic_rate", "valuation_rate")
RATE_TOLERANCE = 0.00001


def _local_se_name(external):
    remote_id = (external.remote_id or external.name or "").strip()
    name = frappe.db.get_value("Stock Entry", {"remote_id": remote_id}, "name")
    if name:
        return name
    return external.name if frappe.db.exists("Stock Entry", external.name) else None


def _rates_differ(local_items, ext_items):
    """
    True when any locally stored rate disagrees with the remote rate for the
    same row. Rows are matched by custom_remote_id (the copied remote row
    name), falling back to idx order.
    """
    ext_by_key = {}
    for idx, row in enumerate(ext_items, start=1):
        ext_by_key[row.name] = row
        ext_by_key.setdefault(f"idx:{idx}", row)

    for idx, row in enumerate(local_items, start=1):
        ext_row = ext_by_key.get(row.get("custom_remote_id")) or ext_by_key.get(f"idx:{idx}")
        if not ext_row:
            continue
        for field in RATE_FIELDS:
            if abs(flt(row.get(field)) - flt(ext_row.get(field))) > RATE_TOLERANCE:
                return True
    return False


def analyze(limit=0):
    """List synced Stock Entries whose rates no longer match the remote."""
    ext_names = frappe.get_all(
        "External Stock Entry",
        filters={"docstatus": ["!=", 2]},
        pluck="name",
        order_by="creation desc",
        limit_page_length=int(limit) or 0,
    )

    mismatched = []
    for ext_name in ext_names:
        ext = frappe.get_doc("External Stock Entry", ext_name)
        local_name = _local_se_name(ext)
        if not local_name or cint(ext.docstatus) == 2:
            continue

        local_docstatus = frappe.db.get_value("Stock Entry", local_name, "docstatus")
        if cint(local_docstatus) != 1:
            continue

        local_items = frappe.get_all(
            "Stock Entry Detail",
            filters={"parent": local_name, "parenttype": "Stock Entry"},
            fields=["name", "idx", "item_code", "custom_remote_id"] + list(RATE_FIELDS),
            order_by="idx",
        )
        if _rates_differ(local_items, ext.items):
            mismatched.append({"external": ext_name, "local": local_name})

    print(f"{len(mismatched)} Stock Entry(ies) need repair")
    for row in mismatched:
        print(f"  {row['local']}  <-  {row['external']}")
    return mismatched


def repair_one(ext_name, company=None):
    """Cancel + delete the local copy and re-sync one External Stock Entry."""
    ext = frappe.get_doc("External Stock Entry", ext_name)
    company = company or ext.company
    local_name = _local_se_name(ext)

    result = {"external": ext_name, "local": local_name}

    if local_name:
        docstatus = cint(frappe.db.get_value("Stock Entry", local_name, "docstatus"))
        se = frappe.get_doc("Stock Entry", local_name)

        if docstatus == 1:
            with temporarily_enable_transaction_items(se):
                se.flags.ignore_permissions = True
                se.cancel()
            result["cancelled"] = True

        if frappe.db.exists("Stock Entry", local_name):
            se = frappe.get_doc("Stock Entry", local_name)
            with temporarily_enable_transaction_items(se):
                se.flags.ignore_permissions = True
                frappe.delete_doc("Stock Entry", local_name, force=True)
            result["deleted"] = True

    from emkan_insights.emkan_insights.external_stock_entry_sync import (
        sync_external_stock_entry,
    )

    outcome = sync_external_stock_entry(ext_name, company)

    result["sync"] = outcome

    new_name = outcome.get("name") if isinstance(outcome, dict) else None
    if new_name and frappe.db.exists("Stock Entry", new_name):
        rates = frappe.get_all(
            "Stock Entry Detail",
            filters={"parent": new_name, "parenttype": "Stock Entry"},
            fields=["item_code", "basic_rate"],
            order_by="idx",
        )
        result["new_rates"] = rates

    print(frappe.as_json(result))
    return result


def repair_all(limit=0, dry_run=False):
    """Analyze then repair every mismatched entry, oldest risk last."""
    mismatched = analyze(limit=limit)
    if dry_run:
        return mismatched

    results = []
    for row in mismatched:
        try:
            results.append(repair_one(row["external"]))
        except Exception:
            frappe.log_error(
                title=f"SE rate repair failed: {row['external']}",
                message=frappe.get_traceback(),
            )
            results.append({"external": row["external"], "error": "see Error Log"})
            frappe.db.rollback()

    ok = sum(1 for r in results if not r.get("error"))
    print(f"Repaired {ok}/{len(results)}")
    return results
