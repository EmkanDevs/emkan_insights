import json
import time
from collections import defaultdict
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
    _acquire_sync_lock,
    _release_sync_lock,
    _run_sync_loop,
    _sort_by_hierarchy,
)

# ⭐ NEW: Chunk size for splitting large bulk syncs into multiple background jobs.
CHUNK_SIZE = 300
FETCH_WORKERS = 8

REMOTE_DT = "Cost Center"
LOCAL_DT = "External Cost Center"
FAILED_NAME_LOG_LIMIT = 100
DATA_FIELD_MAX_LENGTH = 140
PROGRESS_KEY_PREFIX = "cc_sync_progress"


def _fetch_remote_names(base_url, headers, filters):
    url = f"{base_url}/api/resource/{quote(REMOTE_DT, safe='')}"
    names = []
    limit = 1000
    start = 0

    while True:
        params = {
            "fields": json.dumps(["name"]),
            "filters": json.dumps(filters),
            "limit_page_length": limit,
            "limit_start": start,
            "order_by": "name asc",
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
    encoded_dt = quote(REMOTE_DT, safe="")

    def _fetch_one(name):
        encoded_name = quote(str(name), safe="")
        url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json().get("data") or {"name": name}
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


def _get_doc_name(item):
    if not isinstance(item, dict):
        return None
    return item.get("name") or item.get("remote_id")


def _get_cost_center_code(item):
    remote_name = _get_doc_name(item) or ""
    return remote_name.split(" - ", 1)[0].strip() or remote_name.strip()


def _fit_data_field(value):
    value = (value or "").strip()
    if len(value) <= DATA_FIELD_MAX_LENGTH:
        return value
    return value[:DATA_FIELD_MAX_LENGTH].rstrip()


def _with_unique_suffix(base_name, code, used_names):
    suffix = f" ({code})" if code else ""
    if not suffix:
        suffix = f" ({frappe.generate_hash(length=8)})"

    max_base_length = DATA_FIELD_MAX_LENGTH - len(suffix)
    candidate_base = (base_name or "").strip()
    if max_base_length > 0:
        candidate_base = candidate_base[:max_base_length].rstrip()
    candidate = f"{candidate_base}{suffix}"[:DATA_FIELD_MAX_LENGTH]

    counter = 2
    while candidate in used_names:
        retry_suffix = f" ({code}-{counter})" if code else f" ({counter})"
        max_base_length = DATA_FIELD_MAX_LENGTH - len(retry_suffix)
        retry_base = (base_name or "").strip()
        if max_base_length > 0:
            retry_base = retry_base[:max_base_length].rstrip()
        candidate = f"{retry_base}{retry_suffix}"[:DATA_FIELD_MAX_LENGTH]
        counter += 1

    used_names.add(candidate)
    return candidate


def _make_cost_center_names_unique(data):
    groups = defaultdict(list)
    for item in data:
        if not isinstance(item, dict):
            continue
        base_name = _fit_data_field(item.get("cost_center_name") or _get_doc_name(item))
        item["cost_center_name"] = base_name
        groups[base_name].append(item)

    duplicate_bases = {base_name for base_name, items in groups.items() if len(items) > 1}
    if not duplicate_bases:
        return data

    used_names = {
        base_name
        for base_name, items in groups.items()
        if base_name and len(items) == 1
    }
    adjusted = []
    for item in data:
        base_name = item.get("cost_center_name")
        if base_name not in duplicate_bases:
            continue

        unique_name = _with_unique_suffix(base_name, _get_cost_center_code(item), used_names)
        if unique_name != base_name:
            adjusted.append(
                {
                    "remote_id": _get_doc_name(item),
                    "from": base_name,
                    "to": unique_name,
                }
            )
            item["cost_center_name"] = unique_name

    if adjusted:
        frappe.logger("external_sync").warning(
            "Adjusted %s duplicate Cost Center display names before saving",
            len(adjusted),
        )
        frappe.log_error(
            title="Cost Center duplicate display names adjusted",
            message=json.dumps(adjusted, indent=2, default=str),
        )

    return data


def _log_cost_center_result(fetched_total, saved_count, not_saved, errors):
    failed_names = [name for name in (_get_doc_name(item) for item in not_saved) if name]
    summary = (
        f"Cost Center sync result: fetched={fetched_total}, "
        f"saved={saved_count}, not_saved={len(not_saved)}"
    )

    frappe.logger("external_sync").info(summary)
    if failed_names:
        frappe.logger("external_sync").warning(
            "%s. Failed Cost Centers: %s",
            summary,
            ", ".join(failed_names[:FAILED_NAME_LOG_LIMIT]),
        )
        frappe.log_error(
            title="Cost Center sync not saved records",
            message=json.dumps(
                {
                    "summary": summary,
                    "failed_cost_centers": failed_names,
                    "errors": errors,
                },
                indent=2,
                default=str,
            ),
        )


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
def sync_cost_centers_from_remote(site_url, api_key, api_secret, ref_doctype=None, child_docname=None, company=None, configuration_name=None):
    frappe.logger("external_sync").info("Starting Cost Center sync with company %s", company)

    if not _acquire_sync_lock(child_docname):
        frappe.throw(_("A Cost Center sync is already running for this row. Please wait for it to finish."))

    try:
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

        remote_names = _fetch_remote_names(base_url, headers, filters)
        from emkan_insights.emkan_insights.fetch_log import log_fetch_plan
        log_fetch_plan(REMOTE_DT, configuration_name, filters, len(remote_names))
        frappe.logger("external_sync").info(
            "Cost Center name listing fetched %s remote records", len(remote_names)
        )

        if not remote_names:
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

        chunks = [remote_names[i:i + CHUNK_SIZE] for i in range(0, len(remote_names), CHUNK_SIZE)]
        _init_progress(child_docname, len(chunks))

        for idx, chunk in enumerate(chunks):
            frappe.enqueue(
                "emkan_insights.emkan_insights.sync_cost_centers_from_remote.process_cost_center_chunk",
                queue="long",
                timeout=900,
                job_name=f"cc_sync_chunk_{child_docname}_{idx}_{int(time.time())}",
                site_url=site_url,
                api_key=api_key,
                api_secret=api_secret,
                company=company,
                configuration_name=configuration_name,
                child_docname=child_docname,
                remote_names=chunk,
                is_last_chunk=(idx == len(chunks) - 1),
                fetch_started_at=fetch_started_at,
            )

        return {
            "status": "queued",
            "message": (
                f"Queued {len(chunks)} chunk(s) covering {len(remote_names)} Cost Centers. "
                "Please wait for completion before starting again."
            ),
        }

    except Exception:
        _release_sync_lock(child_docname)
        raise


@frappe.whitelist()
def process_cost_center_chunk(
    site_url,
    api_key,
    api_secret,
    company,
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
        docs = _fetch_full_docs_threaded(base_url, headers, remote_names)
        docs = _make_cost_center_names_unique(docs)
        docs = _sort_by_hierarchy(docs, "parent_cost_center")

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

        _log_cost_center_result(len(remote_names), count, not_saved, errors)

        state = _update_progress(child_docname, count, len(not_saved), errors)

    finally:
        if is_last_chunk:
            last_sync = mark_fetch_completed(
                child_docname,
                REMOTE_DT,
                fetch_started_at or frappe.utils.now(),
                record_count=state.get("count", 0),
            )
            _release_sync_lock(child_docname)
            frappe.publish_realtime(
                "cost_center_sync_complete",
                {
                    "child_docname": child_docname,
                    "count": state.get("count"),
                    "not_saved_count": state.get("not_saved_count"),
                    "errors": state.get("errors"),
                    "last_sync": last_sync,
                },
            )
