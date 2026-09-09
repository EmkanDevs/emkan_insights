"""Repair sync-created Stock Entries whose source timestamps were overwritten."""

from collections import Counter
from contextlib import contextmanager

import frappe
from frappe.query_builder.functions import Date
from frappe.utils import cint

from emkan_insights.emkan_insights.sync_guard import (
    temporarily_enable_transaction_items,
)

CONFIRMATION = "REMOVE_MISDATED_STOCK_ENTRIES"
LINKED_CONFIRMATION = "REMOVE_LINKED_STOCK_ENTRIES"
RECOVERY_DOCTYPES = {"Purchase Receipt", "Stock Entry"}


def analyze(company, created_on=None, include_rows=False):
    """Summarize submitted local Stock Entries whose timestamp differs from source."""
    rows = _get_mismatches(company=company, created_on=created_on)
    result = {
        "count": len(rows),
        "created_on": created_on,
        "sample": rows[:20],
    }
    if include_rows:
        result["rows"] = rows
    return result


def analyze_linked(company):
    """Summarize submitted local Stock Entries linked to submitted sources."""
    rows = _get_linked_entries(company)
    return {
        "count": len(rows),
        "first": rows[:5],
        "last": rows[-5:],
    }


def analyze_pending(config_name, ref_doctype):
    """Summarize pending source records selected by the normal scheduler."""
    if ref_doctype not in RECOVERY_DOCTYPES:
        frappe.throw(
            f"Recovery analysis only supports: {', '.join(sorted(RECOVERY_DOCTYPES))}"
        )

    from emkan_insights.emkan_insights.scheduler import (
        get_external_doc_names_to_sync,
    )

    config = frappe.get_doc("External Site Configuration", config_name)
    external_doctype = f"External {ref_doctype}"
    names = get_external_doc_names_to_sync(external_doctype, config=config)
    fields = ["name"]
    meta = frappe.get_meta(external_doctype)
    fields.extend(
        fieldname
        for fieldname in ("posting_date", "posting_time", "docstatus")
        if meta.has_field(fieldname)
    )
    samples = {
        row.name: row
        for row in frappe.get_all(
            external_doctype,
            filters={"name": ["in", names[:5] + names[-5:]]},
            fields=fields,
        )
    }
    return {
        "count": len(names),
        "first": [samples[name] for name in names[:5] if name in samples],
        "last": [samples[name] for name in names[-5:] if name in samples],
    }


def remove_mismatches(company, created_on=None, confirm=None):
    """ORM-cancel/delete timestamp mismatches and reset their source markers."""
    rows = _get_mismatches(company=company, created_on=created_on)
    if confirm != CONFIRMATION:
        return {
            "status": "dry_run",
            "count": len(rows),
            "confirmation_required": CONFIRMATION,
        }

    return _remove_rows(rows)


def remove_mismatch(external_name, confirm=None):
    """Remove one confirmed timestamp mismatch for a guarded pilot/retry."""
    external = frappe.db.get_value(
        "External Stock Entry",
        external_name,
        ["company", "docstatus"],
        as_dict=True,
    )
    if not external or cint(external.docstatus) != 1:
        frappe.throw(f"Submitted External Stock Entry {external_name} was not found")

    rows = [
        row
        for row in _get_mismatches(company=external.company)
        if row.external_name == external_name
    ]
    if not rows:
        return {"status": "not_mismatched", "external_name": external_name}
    if confirm != CONFIRMATION:
        return {
            "status": "dry_run",
            "count": 1,
            "confirmation_required": CONFIRMATION,
        }

    return _remove_rows(rows)


def remove_linked(company, confirm=None):
    """Remove all active local Stock Entries linked to external sources."""
    rows = _get_linked_entries(company)
    if confirm != LINKED_CONFIRMATION:
        return {
            "status": "dry_run",
            "count": len(rows),
            "confirmation_required": LINKED_CONFIRMATION,
        }

    return _remove_rows(rows)


def sync_pending(config_name, ref_doctype):
    """Synchronously replay one supported transaction type in source order."""
    if ref_doctype not in RECOVERY_DOCTYPES:
        frappe.throw(
            f"Recovery sync only supports: {', '.join(sorted(RECOVERY_DOCTYPES))}"
        )

    from emkan_insights.emkan_insights.registry.sync_registry import SYNC_METHODS
    from emkan_insights.emkan_insights.scheduler import (
        _sync_records_for_sequence_row,
        get_external_doc_names_to_sync,
    )

    config = frappe.get_doc("External Site Configuration", config_name)
    external_doctype = f"External {ref_doctype}"
    names = get_external_doc_names_to_sync(external_doctype, config=config)
    if not names:
        return {
            "status": "nothing_pending",
            "ref_doctype": ref_doctype,
            "count": 0,
        }

    results = _sync_records_for_sequence_row(
        SYNC_METHODS[ref_doctype],
        row=frappe._dict({"ref_doctype": ref_doctype}),
        external_doctype=external_doctype,
        names=names,
        config=config,
    )
    statuses = Counter(
        str(result.get("status") or "unknown")
        for result in results
        if isinstance(result, dict)
    )
    return {
        "status": "completed",
        "ref_doctype": ref_doctype,
        "count": len(names),
        "first_name": names[0],
        "last_name": names[-1],
        "result_statuses": dict(statuses),
    }


def _remove_rows(rows):
    original_allow_negative = cint(
        frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
    )
    removed = []
    try:
        _set_allow_negative_stock(1)
        for row in reversed(rows):
            _remove_one(row.local_name)
            _reset_external_sync_marker(row.external_name)
            removed.append(row.external_name)
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        _set_allow_negative_stock(original_allow_negative)

    return {
        "status": "removed",
        "count": len(removed),
        "external_names": removed,
        "allow_negative_stock_restored_to": original_allow_negative,
    }


def _get_mismatches(company, created_on=None):
    external = frappe.qb.DocType("External Stock Entry")
    local = frappe.qb.DocType("Stock Entry")

    query = (
        frappe.qb.from_(external)
        .join(local)
        .on(local.remote_id == external.remote_id)
        .select(
            external.name.as_("external_name"),
            external.posting_date.as_("external_posting_date"),
            external.posting_time.as_("external_posting_time"),
            local.name.as_("local_name"),
            local.posting_date.as_("local_posting_date"),
            local.posting_time.as_("local_posting_time"),
            local.creation.as_("local_creation"),
        )
        .where(external.company == company)
        .where(local.company == company)
        .where(external.docstatus == 1)
        .where(local.docstatus == 1)
        .where(
            (local.posting_date != external.posting_date)
            | (local.posting_time != external.posting_time)
        )
        .orderby(external.posting_date)
        .orderby(external.posting_time)
        .orderby(external.name)
    )
    if created_on:
        query = query.where(Date(local.creation) == created_on)

    return query.run(as_dict=True)


def _get_linked_entries(company):
    external = frappe.qb.DocType("External Stock Entry")
    local = frappe.qb.DocType("Stock Entry")

    return (
        frappe.qb.from_(external)
        .join(local)
        .on(local.remote_id == external.remote_id)
        .select(
            external.name.as_("external_name"),
            external.docstatus.as_("external_docstatus"),
            external.posting_date.as_("external_posting_date"),
            external.posting_time.as_("external_posting_time"),
            local.name.as_("local_name"),
        )
        .where(external.company == company)
        .where(local.company == company)
        .where(local.docstatus == 1)
        .orderby(external.posting_date)
        .orderby(external.posting_time)
        .orderby(external.name)
    ).run(as_dict=True)


def _remove_one(local_name):
    doc = frappe.get_doc("Stock Entry", local_name)
    with _temporarily_enable_warehouses(doc):
        with temporarily_enable_transaction_items(doc):
            doc.flags.ignore_permissions = True
            doc.cancel()

    frappe.delete_doc(
        "Stock Entry",
        local_name,
        force=True,
        ignore_permissions=True,
    )


def _reset_external_sync_marker(external_name):
    values = {"last_synced_on": None}
    if frappe.get_meta("External Stock Entry").has_field("is_synced"):
        values["is_synced"] = 0
    frappe.db.set_value(
        "External Stock Entry",
        external_name,
        values,
        update_modified=False,
    )


def _set_allow_negative_stock(value):
    frappe.db.set_single_value("Stock Settings", "allow_negative_stock", cint(value))


@contextmanager
def _temporarily_enable_warehouses(doc):
    warehouses = {
        warehouse
        for row in doc.get("items", [])
        for warehouse in (row.get("s_warehouse"), row.get("t_warehouse"))
        if warehouse
    }
    disabled = [
        warehouse
        for warehouse in warehouses
        if frappe.db.get_value("Warehouse", warehouse, "disabled")
    ]

    try:
        for warehouse in disabled:
            frappe.db.set_value(
                "Warehouse",
                warehouse,
                "disabled",
                0,
                update_modified=False,
            )
        yield
    finally:
        for warehouse in disabled:
            frappe.db.set_value(
                "Warehouse",
                warehouse,
                "disabled",
                1,
                update_modified=False,
            )
