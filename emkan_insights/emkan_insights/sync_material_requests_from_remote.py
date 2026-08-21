import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
import requests
from frappe import _

from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
    _build_company_prefixed_remote_id,
    _get_company_abbr_for_sync,
    _run_sync_loop,
    _acquire_sync_lock,
    _release_sync_lock,
)

# Tune these based on remote API rate limits / worker capacity.
CHUNK_SIZE = 300          # remote records processed per background job
FETCH_WORKERS = 8         # concurrent HTTP calls when pulling full docs w/ children

REMOTE_DT = "Material Request"
LOCAL_DT = "External Material Request"
PROGRESS_KEY_PREFIX = "mr_sync_progress"


def _prepare_material_request_for_external_sync(item, company_abbr):
    item = dict(item or {})
    original_remote_id = item.get("name") or item.get("id")
    prefixed_remote_id = _build_company_prefixed_remote_id(company_abbr, original_remote_id)

    if prefixed_remote_id:
        item["name"] = prefixed_remote_id
        item["id"] = prefixed_remote_id

    if item.get("amended_from"):
        item["amended_from"] = _build_company_prefixed_remote_id(company_abbr, item.get("amended_from"))

    return item


def _fetch_remote_names(base_url, headers, remote_dt, filters):
    """
    Cheap listing pass: only pull `name` (not fields=["*"]).
    17k rows of just names is a fraction of the payload of full records,
    and lets us plan chunks before doing any heavy per-record fetching.
    """
    url = f"{base_url}/api/resource/{remote_dt}"
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


def _fetch_full_docs_threaded(base_url, headers, remote_dt, names):
    """
    Threaded replacement for _fetch_remote_docs_with_children, scoped to a
    single chunk of names. Sequential per-record GETs are the dominant
    cost in the original flow; this bounds concurrency instead of doing
    them one at a time.
    """
    docs = []

    def _fetch_one(name):
        from urllib.parse import quote
        encoded_dt = quote(str(remote_dt), safe="")
        encoded_name = quote(str(name), safe="")
        url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json().get("data") or {"name": name}
        except Exception as e:
            frappe.logger("external_sync").warning(
                "Failed to fetch full doc for %s/%s: %s", remote_dt, name, str(e)
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


def _enqueue_material_request_chunk(
    *,
    site_url,
    api_key,
    api_secret,
    company,
    company_abbr,
    configuration_name,
    child_docname,
    chunks,
    chunk_index,
    fetch_started_at,
):
    if chunk_index >= len(chunks):
        return

    frappe.enqueue(
        "emkan_insights.emkan_insights.sync_material_requests_from_remote.process_material_request_chunk",
        queue="long",
        timeout=900,
        job_name=f"mr_sync_chunk_{child_docname}_{chunk_index}",
        site_url=site_url,
        api_key=api_key,
        api_secret=api_secret,
        company=company,
        company_abbr=company_abbr,
        configuration_name=configuration_name,
        child_docname=child_docname,
        chunks=chunks,
        chunk_index=chunk_index,
        fetch_started_at=fetch_started_at,
    )


@frappe.whitelist()
def sync_material_requests_from_remote(site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None):
    """
    Lightweight entry point. Only lists remote names, splits into chunks,
    and enqueues one background job per chunk. Kept fast so it can safely
    be called directly (it's already invoked via frappe.enqueue from
    sync__docs, but is also cheap enough to call synchronously if needed).
    """
    if not _acquire_sync_lock(child_docname):
        frappe.throw(_("A Material Request sync is already running for this row. Please wait for it to finish."))

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

        filters = [
            ["docstatus", "in", [0, 1, 2]]
        ]
        # if previous_last_sync:
        #     filters.append([
        #         "modified",
        #         ">",
        #         frappe.utils.get_datetime_str(previous_last_sync)
        #     ])

        if not frappe.db.exists("DocType", LOCAL_DT):
            frappe.throw(
                _("Local DocType {0} is not installed on this site. Run bench migrate and reload.")
                .format(LOCAL_DT)
            )

        names = _fetch_remote_names(base_url, headers, REMOTE_DT, filters)

        if not names:
            _release_sync_lock(child_docname)
            last_sync = fetch_started_at
            frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
            return {"count": 0, "fetched_total": 0, "not_saved_count": 0, "last_sync": last_sync, "errors": [], "missing_parent_accounts": []}

        chunks = [names[i:i + CHUNK_SIZE] for i in range(0, len(names), CHUNK_SIZE)]
        _init_progress(child_docname, len(chunks))

        _enqueue_material_request_chunk(
            site_url=site_url,
            api_key=api_key,
            api_secret=api_secret,
            company=company,
            company_abbr=company_abbr,
            configuration_name=configuration_name,
            child_docname=child_docname,
            chunks=chunks,
            chunk_index=0,
            fetch_started_at=fetch_started_at,
        )

        return f"Queued {len(chunks)} chunk(s) covering {len(names)} Material Requests"

    except Exception:
        _release_sync_lock(child_docname)
        raise


@frappe.whitelist()
def process_material_request_chunk(
    site_url,
    api_key,
    api_secret,
    company,
    company_abbr,
    configuration_name,
    child_docname,
    remote_names=None,
    is_last_chunk=False,
    fetch_started_at=None,
    chunks=None,
    chunk_index=0,
):
    """
    Background worker for a single chunk of Material Request names.
    Bounded timeout regardless of total remote volume, since chunk size
    is fixed rather than the full 17k dataset.
    """
    base_url = (site_url or "").rstrip("/")
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
    }
    state = {}
    chunks = chunks or [remote_names or []]
    chunk_index = int(chunk_index or 0)
    remote_names = chunks[chunk_index] if chunk_index < len(chunks) else []

    try:
        docs = _fetch_full_docs_threaded(base_url, headers, REMOTE_DT, remote_names)
        docs = [_prepare_material_request_for_external_sync(item, company_abbr) for item in docs]

        local_meta = frappe.get_meta(LOCAL_DT)
        local_fields = [df.fieldname for df in local_meta.fields]

        count, errors, not_saved = _run_sync_loop(
            docs, LOCAL_DT, local_fields, company, configuration_name, site_url
        )

        if not_saved:
            retry_count, retry_errors, not_saved = _run_sync_loop(
                not_saved, LOCAL_DT, local_fields, company, configuration_name, site_url
            )
            count += retry_count
            errors.extend(retry_errors)

        state = _update_progress(child_docname, count, len(not_saved), errors)

    finally:
        next_chunk_index = chunk_index + 1
        if next_chunk_index < len(chunks):
            _enqueue_material_request_chunk(
                site_url=site_url,
                api_key=api_key,
                api_secret=api_secret,
                company=company,
                company_abbr=company_abbr,
                configuration_name=configuration_name,
                child_docname=child_docname,
                chunks=chunks,
                chunk_index=next_chunk_index,
                fetch_started_at=fetch_started_at,
            )
        else:
            last_sync = fetch_started_at or frappe.utils.now()
            frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
            _release_sync_lock(child_docname)
            frappe.publish_realtime(
                "material_request_sync_complete",
                {
                    "child_docname": child_docname,
                    "count": state.get("count"),
                    "not_saved_count": state.get("not_saved_count"),
                    "errors": state.get("errors"),
                    "last_sync": last_sync,
                },
            )
