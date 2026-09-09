"""Consistent fetch-window timestamps and filters for external data."""

import frappe


def mark_fetch_started(child_docname, ref_doctype, window_start, window_end):
    window_start = _as_datetime_string(window_start)
    window_end = _as_datetime_string(window_end)
    frappe.db.set_value(
        "External Site Configuration CT",
        child_docname,
        {
            "fetch_started_at": window_end,
            "fetch_completed_at": None,
        },
        update_modified=False,
    )
    _log_progress(
        f"Fetching {ref_doctype}",
        child_docname,
        window_start,
        window_end,
    )
    frappe.db.commit()
    return window_start, window_end


def add_fetch_window_end(filters, window_end):
    filters.append(["modified", "<=", _as_datetime_string(window_end)])
    return filters


def mark_fetch_completed(child_docname, ref_doctype, window_end, record_count=None):
    window_end = _as_datetime_string(window_end)
    completed_at = frappe.utils.now()
    frappe.db.set_value(
        "External Site Configuration CT",
        child_docname,
        {
            "last_sync": window_end,
            "fetch_completed_at": completed_at,
        },
        update_modified=False,
    )
    count_text = f" | Records saved: {record_count:,}" if record_count is not None else ""
    _log_progress(
        f"Fetched {ref_doctype}",
        child_docname,
        None,
        window_end,
        f"Completed at: {completed_at}{count_text}",
    )
    frappe.db.commit()
    return window_end


def _log_progress(title, child_docname, window_start, window_end, suffix=None):
    parts = [f"Row: {child_docname}"]
    if window_start:
        parts.append(f"Window start: {window_start}")
    parts.append(f"Window end: {window_end}")
    if suffix:
        parts.append(suffix)
    message = " | ".join(parts)
    frappe.logger("external_sync").info("%s | %s", title, message)
    frappe.log_error(
        title=f"External Fetch Progress: {title}"[:140],
        message=message,
    )


def _as_datetime_string(value):
    if not value:
        return None
    return frappe.utils.get_datetime_str(value)
