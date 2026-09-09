"""Fast, bounded fetch path for high-volume ledger rows."""

import json
import time
from urllib.parse import quote

import frappe
import requests

from emkan_insights.emkan_insights.ledger_storage import upsert_ledger_page


LEDGER_DOCTYPE_MAPPINGS = {
    "GL Entry": "External GL Entry",
    "Period Closing Voucher": "External Period Closing Voucher",
    "Stock Ledger Entry": "External Stock Ledger Entry",
    "Payment Ledger Entry": "External Payment Ledger Entry",
}
LEDGER_PAGE_SIZE = 1_000
LEDGER_PROGRESS_INTERVAL = 10_000
REMOTE_REQUEST_ATTEMPTS = 3


def queue_ledger_fetch(
    *,
    site_url,
    api_key,
    api_secret,
    company,
    configuration_name,
    child_docname,
    ref_doctype,
    window_start,
    window_end,
):
    """Start a fixed-window, cursor-paginated ledger fetch."""
    local_doctype = LEDGER_DOCTYPE_MAPPINGS[ref_doctype]
    company_abbr = _get_company_abbr(company)
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
        _progress_message(configuration_name, window_start, window_end, 0),
    )
    frappe.db.commit()

    _enqueue_ledger_page(
        site_url=site_url,
        api_key=api_key,
        api_secret=api_secret,
        company_abbr=company_abbr,
        configuration_name=configuration_name,
        child_docname=child_docname,
        ref_doctype=ref_doctype,
        local_doctype=local_doctype,
        window_start=window_start,
        window_end=window_end,
        cursor=None,
        fetched_count=0,
    )
    return {
        "status": "queued",
        "message": f"Queued fast paged fetch for {ref_doctype}",
        "window_start": window_start,
        "window_end": window_end,
    }


def process_ledger_page(
    *,
    site_url,
    api_key,
    api_secret,
    company_abbr,
    configuration_name,
    child_docname,
    ref_doctype,
    local_doctype,
    window_start,
    window_end,
    cursor=None,
    fetched_count=0,
):
    """Fetch and upsert one page, then enqueue the next cursor."""
    fetched_count = int(fetched_count or 0)

    try:
        rows = _fetch_ledger_page(
            site_url=site_url,
            api_key=api_key,
            api_secret=api_secret,
            ref_doctype=ref_doctype,
            window_start=window_start,
            window_end=window_end,
            cursor=cursor,
        )
        upsert_ledger_page(
            local_doctype=local_doctype,
            rows=rows,
            company_abbr=company_abbr,
            configuration_name=configuration_name,
        )
        fetched_count += len(rows)
        frappe.db.commit()

        if len(rows) == LEDGER_PAGE_SIZE:
            next_cursor = rows[-1].get("name")
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError(f"Ledger cursor did not advance after {cursor!r}")

            _log_periodic_progress(
                ref_doctype,
                configuration_name,
                window_start,
                window_end,
                fetched_count,
            )
            _enqueue_ledger_page(
                site_url=site_url,
                api_key=api_key,
                api_secret=api_secret,
                company_abbr=company_abbr,
                configuration_name=configuration_name,
                child_docname=child_docname,
                ref_doctype=ref_doctype,
                local_doctype=local_doctype,
                window_start=window_start,
                window_end=window_end,
                cursor=next_cursor,
                fetched_count=fetched_count,
            )
            return {"status": "running", "count": fetched_count}

        return _complete_ledger_fetch(
            child_docname=child_docname,
            ref_doctype=ref_doctype,
            configuration_name=configuration_name,
            window_start=window_start,
            window_end=window_end,
            fetched_count=fetched_count,
        )
    except Exception:
        frappe.db.rollback()
        _release_row_lock(child_docname)
        error_message = frappe.get_traceback()
        frappe.logger("external_sync").error(
            "Fast ledger fetch failed for %s/%s after %d records: %s",
            ref_doctype,
            child_docname,
            fetched_count,
            error_message,
        )
        frappe.log_error(
            title=f"External ledger fetch failed: {ref_doctype}"[:140],
            message=error_message,
        )
        frappe.publish_realtime(
            "generic_sync_complete",
            {
                "child_docname": child_docname,
                "ref_doctype": ref_doctype,
                "count": fetched_count,
                "not_saved_count": 1,
                "errors": [{"remote_id": cursor, "error": error_message}],
                "last_sync": window_start,
            },
        )
        raise


def _fetch_ledger_page(
    *,
    site_url,
    api_key,
    api_secret,
    ref_doctype,
    window_start,
    window_end,
    cursor,
):
    filters = _build_ledger_filters(window_start, window_end, cursor)
    url = f"{site_url.rstrip('/')}/api/resource/{quote(ref_doctype, safe='')}"
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
    }
    params = {
        "fields": json.dumps(["*"]),
        "filters": json.dumps(filters),
        "limit_page_length": LEDGER_PAGE_SIZE,
        "order_by": "name asc",
    }

    for attempt in range(1, REMOTE_REQUEST_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=90)
            response.raise_for_status()
            return response.json().get("data", [])
        except requests.RequestException:
            if attempt == REMOTE_REQUEST_ATTEMPTS:
                raise
            time.sleep(attempt)

    return []


def _build_ledger_filters(window_start, window_end, cursor=None):
    filters = [["modified", "<=", _as_datetime_string(window_end)]]
    if window_start:
        filters.insert(0, ["modified", ">", _as_datetime_string(window_start)])
    if cursor:
        filters.append(["name", ">", cursor])
    return filters


def _enqueue_ledger_page(**kwargs):
    frappe.enqueue(
        "emkan_insights.emkan_insights.ledger_fetch.process_ledger_page",
        queue="long",
        timeout=900,
        job_name=(
            f"external_ledger_fetch_{kwargs['child_docname']}_"
            f"{kwargs.get('fetched_count', 0)}"
        ),
        **kwargs,
    )


def _complete_ledger_fetch(
    *,
    child_docname,
    ref_doctype,
    configuration_name,
    window_start,
    window_end,
    fetched_count,
):
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
    _log_progress(
        f"Fetched {ref_doctype}",
        _progress_message(
            configuration_name,
            window_start,
            window_end,
            fetched_count,
            completed_at,
        ),
    )
    frappe.db.commit()
    _release_row_lock(child_docname)

    result = {
        "count": fetched_count,
        "fetched_total": fetched_count,
        "not_saved_count": 0,
        "last_sync": window_end,
        "fetch_started_at": window_end,
        "fetch_completed_at": completed_at,
        "errors": [],
    }
    frappe.publish_realtime(
        "generic_sync_complete",
        {"child_docname": child_docname, "ref_doctype": ref_doctype, **result},
    )
    return result


def _log_periodic_progress(
    ref_doctype,
    configuration_name,
    window_start,
    window_end,
    fetched_count,
):
    if fetched_count % LEDGER_PROGRESS_INTERVAL:
        return
    _log_progress(
        f"Fetching {ref_doctype}: {fetched_count:,}",
        _progress_message(
            configuration_name,
            window_start,
            window_end,
            fetched_count,
        ),
    )


def _log_progress(title, message):
    frappe.logger("external_sync").info("%s | %s", title, message)
    frappe.log_error(
        title=f"External Fetch Progress: {title}"[:140],
        message=message,
    )


def _progress_message(
    configuration_name,
    window_start,
    window_end,
    fetched_count,
    completed_at=None,
):
    parts = [
        f"Site: {configuration_name}",
        f"Window start: {window_start or 'beginning'}",
        f"Window end: {window_end}",
        f"Records saved: {fetched_count:,}",
    ]
    if completed_at:
        parts.append(f"Completed at: {completed_at}")
    return " | ".join(parts)


def _release_row_lock(child_docname):
    from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
        _release_sync_lock,
    )

    _release_sync_lock(child_docname)


def _get_company_abbr(company):
    if not company:
        frappe.throw("Company is required for ledger fetch.")
    company_abbr = frappe.db.get_value("Company", company, "abbr")
    if not company_abbr:
        frappe.throw(f"Company {company} has no abbreviation set.")
    return company_abbr


def _as_datetime_string(value):
    if not value:
        return None
    return frappe.utils.get_datetime_str(value)
