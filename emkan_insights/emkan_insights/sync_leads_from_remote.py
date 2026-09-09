import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import frappe
import requests
from frappe import _

from emkan_insights.emkan_insights.fetch_window import (
    add_fetch_window_end,
    mark_fetch_completed,
    mark_fetch_started,
)
from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
    _build_company_prefixed_remote_id,
    # _fetch_remote_docs_with_children,  # Removed: redundant, _fetch_full_docs_threaded already gets full doc
    _get_company_abbr_for_sync,
    _run_sync_loop,
    _acquire_sync_lock,
    _release_sync_lock,
)

CHUNK_SIZE = 300
FETCH_WORKERS = 8

REMOTE_DT = "Lead"
LOCAL_DT = "External Lead"
PROGRESS_KEY_PREFIX = "lead_sync_progress"


def _prepare_lead_for_external_sync(item, company_abbr):
    """
    Same pattern as _prepare_quotation_for_external_sync.
    Prefixes the remote ID with the company abbreviation to avoid overwriting.
    """
    item = dict(item or {})
    original_remote_id = item.get("name") or item.get("id")
    prefixed_remote_id = _build_company_prefixed_remote_id(company_abbr, original_remote_id)

    if prefixed_remote_id:
        item["name"] = prefixed_remote_id
        item["id"] = prefixed_remote_id

    return item


def _fetch_remote_names(base_url, headers, filters):
    url = f"{base_url}/api/resource/{REMOTE_DT}"
    names = []
    limit = 1000
    start = 0

    while True:
        params = {
            "fields": json.dumps(["name"]),
            "filters": json.dumps(filters),
            "limit_page_length": limit,
            "limit_start": start,
        }
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        page = response.json().get("data", [])
        if not page:
            break
        names.extend(row["name"] for row in page if row.get("name"))
        if len(page) < limit:
            break
        start += limit

    return names


def _fetch_full_docs_threaded(base_url, headers, names):
    docs = []

    def _fetch_one(name):
        encoded_dt = quote(str(REMOTE_DT), safe="")
        encoded_name = quote(str(name), safe="")
        url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json().get("data") or {"name": name}
        except Exception as e:
            frappe.logger("external_sync").warning(
                "Failed to fetch full doc for %s/%s: %s", REMOTE_DT, name, str(e)
            )
            return {"name": name}

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, name): name for name in names}
        for future in as_completed(futures):
            docs.append(future.result())

    return docs


def _progress_key(child_docname):
    return f"{PROGRESS_KEY_PREFIX}:{child_docname}"


def _init_progress(child_docname, total_chunks):
    frappe.cache().set_value(_progress_key(child_docname), json.dumps({
        "total_chunks": total_chunks,
        "completed_chunks": 0,
        "count": 0,
        "not_saved_count": 0,
        "errors": [],
    }))


def _update_progress(child_docname, count_delta, not_saved_delta, chunk_errors):
    key = _progress_key(child_docname)
    raw = frappe.cache().get_value(key)
    state = json.loads(raw) if raw else {
        "total_chunks": 0, "completed_chunks": 0, "count": 0,
        "not_saved_count": 0, "errors": [],
    }
    state["completed_chunks"] += 1
    state["count"] += count_delta
    state["not_saved_count"] += not_saved_delta
    state["errors"].extend(chunk_errors)
    frappe.cache().set_value(key, json.dumps(state))
    return state


@frappe.whitelist()
def sync_leads_from_remote(
    site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None
):
    if not _acquire_sync_lock(child_docname):
        # When triggered from the scheduler, frappe.throw() raises a ValidationError
        # that the scheduler's except-block catches silently, preventing any RQ jobs
        # from being enqueued. Return gracefully instead.
        frappe.log_error(
            f"Lead sync lock is already held for child row '{child_docname}'. "
            "A previous sync may still be running or crashed without releasing. "
            "Skipping this run.",
            "Lead Sync Lock Conflict",
        )
        return {
            "status": "skipped",
            "message": f"Lead sync already running for row '{child_docname}'. Try again later.",
        }

    try:
        company_abbr = _get_company_abbr_for_sync(company)
        base_url = (site_url or "").rstrip("/")
        headers = {
            "Authorization": f"token {api_key}:{api_secret}",
            "Content-Type": "application/json",
        }

        fetch_started_at = frappe.utils.now()

        previous_last_sync = frappe.db.get_value(
            "External Site Configuration CT",
            child_docname,
            "last_sync"
        )
        previous_last_sync, fetch_started_at = mark_fetch_started(
            child_docname, REMOTE_DT, previous_last_sync, fetch_started_at
        )

        filters = [
            ["docstatus", "in", [0, 1, 2]]
        ]
        # Incremental fetch: only pull records modified since last fetch.
        if previous_last_sync:
            filters.append([
                "modified",
                ">",
                frappe.utils.get_datetime_str(previous_last_sync)
            ])
        add_fetch_window_end(filters, fetch_started_at)

        if not frappe.db.exists("DocType", LOCAL_DT):
            frappe.throw(
                _("Local DocType {0} is not installed on this site. Run bench migrate and reload.")
                .format(LOCAL_DT)
            )

        names = _fetch_remote_names(base_url, headers, filters)
        from emkan_insights.emkan_insights.fetch_log import log_fetch_plan
        log_fetch_plan(REMOTE_DT, configuration_name, filters, len(names))

        if not names:
            _release_sync_lock(child_docname)
            last_sync = mark_fetch_completed(
                child_docname, REMOTE_DT, fetch_started_at, record_count=0
            )
            return {
                "count": 0,
                "fetched_total": 0,
                "not_saved_count": 0,
                "last_sync": last_sync,
                "errors": [],
                "missing_parent_accounts": [],
            }

        chunks = [names[i:i + CHUNK_SIZE] for i in range(0, len(names), CHUNK_SIZE)]
        _init_progress(child_docname, len(chunks))

        for idx, chunk in enumerate(chunks):
            frappe.enqueue(
                "emkan_insights.emkan_insights.sync_leads_from_remote.process_lead_chunk",
                queue="long",
                timeout=900,
                job_name=f"lead_sync_chunk_{child_docname}_{idx}_{int(time.time())}",
                site_url=site_url,
                api_key=api_key,
                api_secret=api_secret,
                company=company,
                company_abbr=company_abbr,
                configuration_name=configuration_name,
                child_docname=child_docname,
                remote_names=chunk,
                is_last_chunk=(idx == len(chunks) - 1),
                fetch_started_at=fetch_started_at,
            )

        return {
            "status": "queued",
            "message": (
                f"Queued {len(chunks)} chunk(s) covering {len(names)} Leads. "
                "Please wait for completion before starting again."
            ),
        }

    except Exception:
        _release_sync_lock(child_docname)
        raise


@frappe.whitelist()
def process_lead_chunk(
    site_url,
    api_key,
    api_secret,
    company,
    company_abbr,
    configuration_name,
    child_docname,
    remote_names,
    is_last_chunk=False,
    fetch_started_at=None,
):
    base_url = (site_url or "").rstrip("/")
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
    }
    state = {}

    try:
        # 1. Fetch full documents for the chunk (includes child tables)
        docs = _fetch_full_docs_threaded(base_url, headers, remote_names)
        
        # 2. Prefix remote IDs
        docs = [_prepare_lead_for_external_sync(item, company_abbr) for item in docs]

        local_meta = frappe.get_meta(LOCAL_DT)
        local_fields = [df.fieldname for df in local_meta.fields]

        # 3. Run Sync Loop
        count, errors, not_saved = _run_sync_loop(
            docs, LOCAL_DT, local_fields, company, configuration_name, site_url
        )

        # 4. Retry if needed
        if not_saved:
            retry_count, retry_errors, not_saved = _run_sync_loop(
                not_saved, LOCAL_DT, local_fields, company, configuration_name, site_url
            )
            count += retry_count
            errors.extend(retry_errors)

        state = _update_progress(child_docname, count, len(not_saved), errors)

    finally:
        if is_last_chunk:
            last_sync = mark_fetch_completed(
                child_docname,
                REMOTE_DT,
                fetch_started_at or frappe.utils.now(),
                record_count=(state or {}).get("count", 0),
            )
            _release_sync_lock(child_docname)
            frappe.publish_realtime(
                "lead_sync_complete",
                {
                    "child_docname": child_docname,
                    "count": state.get("count"),
                    "not_saved_count": state.get("not_saved_count"),
                    "errors": state.get("errors"),
                    "last_sync": last_sync,
                },
            )
