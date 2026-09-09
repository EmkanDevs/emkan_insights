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
    _get_company_abbr_for_sync,
    _run_sync_loop,
    _acquire_sync_lock,
    _release_sync_lock,
)

# ---------------------------------------------------------------------------
# NOTE ON TRANSACTIONS
# ---------------------------------------------------------------------------
# Persistence is left entirely to Frappe's own lifecycle:
#   - sync_journal_entries_from_remote is a whitelisted request; Frappe
#     commits automatically once it returns without an uncaught exception.
#   - process_journal_entry_chunk runs as a background job (frappe.enqueue);
#     the job runner commits automatically on a clean return and rolls back
#     the whole job if an exception propagates out.
#
# Per-item isolation (so one bad Journal Entry doesn't roll back every other
# item already processed in the same chunk) is handled inside _run_sync_loop
# via DB savepoints in _process_item (external_site_configuration.py).
#
# Explicit commits would break the savepoint isolation used by _run_sync_loop.
# ---------------------------------------------------------------------------

# Tune these based on remote API rate limits / worker capacity.
CHUNK_SIZE = 200          # remote records processed per background job
                          # (Journal Entries can have many account rows, so
                          # smaller chunks than MR/PR to stay within timeouts)
FETCH_WORKERS = 8         # concurrent HTTP calls when pulling full docs

REMOTE_DT = "Journal Entry"
LOCAL_DT = "External Journal Entry"
PROGRESS_KEY_PREFIX = "je_sync_progress"


# ---------------------------------------------------------------------------
# DATA PREPARATION
# ---------------------------------------------------------------------------

def _prepare_journal_entry_for_external_sync(item, company_abbr):
    """
    Normalise a raw remote Journal Entry dict before handing it to
    _run_sync_loop / _process_item:

    - Keep item["name"] / item["id"] as the raw remote name so that
      _process_item can look up or build the prefixed local name.
    - Prefix amended_from with the company abbreviation so amendment
      chains resolve correctly after sync.
    - The accounts child table is preserved as-is; _process_item's
      _prepare_child_rows_for_sync strips system fields (name, parent,
      parenttype, parentfield, idx, docstatus, owner, ...) before insert,
      which is exactly what we want: every account row is re-created
      fresh from the remote data, preventing duplicate / orphan rows.
    """
    item = dict(item or {})

    # Prefix amended_from so the local amendment chain points to the
    # correctly-prefixed local JE name (same pattern as MR / PR / PI).
    if item.get("amended_from"):
        item["amended_from"] = _build_company_prefixed_remote_id(
            company_abbr, item["amended_from"]
        )

    return item


# ---------------------------------------------------------------------------
# REMOTE FETCHING
# ---------------------------------------------------------------------------

def _fetch_remote_names(base_url, headers, filters):
    """
    Cheap listing pass: pull only `name` from the remote Journal Entry list.

    Fetching just names first is a tiny fraction of the payload of full
    records (which include all account rows), and lets us plan chunk
    boundaries before any heavy per-record HTTP work begins.
    """
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
            "order_by": "posting_date asc, name asc",
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
    """
    Fetch full Journal Entry documents (including the accounts child table)
    from the remote site using concurrent HTTP requests.

    Using the individual-record endpoint (/api/resource/{dt}/{name}) is
    the only reliable way to get child-table rows -- the list endpoint
    (/api/resource/{dt}) never returns child tables regardless of the
    fields= parameter.  This is the root cause of the missing account rows
    in earlier bulk-list-based approaches.

    Per-record failures are swallowed and logged so that a single
    unreachable document does not abort the entire chunk.
    """
    docs = []
    encoded_dt = quote(REMOTE_DT, safe="")

    def _fetch_one(name):
        encoded_name = quote(str(name), safe="")
        url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
        try:
            resp = requests.get(url, headers=headers, timeout=60)
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


# ---------------------------------------------------------------------------
# PROGRESS TRACKING
# ---------------------------------------------------------------------------

def _progress_key(child_docname):
    return f"{PROGRESS_KEY_PREFIX}:{child_docname}"


def _init_progress(child_docname, total_chunks):
    frappe.cache().set_value(
        _progress_key(child_docname),
        json.dumps({
            "total_chunks": total_chunks,
            "completed_chunks": 0,
            "count": 0,
            "not_saved_count": 0,
            "errors": [],
        }),
    )


def _update_progress(child_docname, count_delta, not_saved_delta, chunk_errors):
    key = _progress_key(child_docname)
    raw = frappe.cache().get_value(key)
    state = json.loads(raw) if raw else {
        "total_chunks": 0,
        "completed_chunks": 0,
        "count": 0,
        "not_saved_count": 0,
        "errors": [],
    }
    state["completed_chunks"] += 1
    state["count"] += count_delta
    state["not_saved_count"] += not_saved_delta
    state["errors"].extend(chunk_errors)
    frappe.cache().set_value(key, json.dumps(state))
    return state


# ---------------------------------------------------------------------------
# ENTRY POINT -- called from the UI / External Site Configuration sync button
# ---------------------------------------------------------------------------

@frappe.whitelist()
def sync_journal_entries_from_remote(
    site_url, api_key, api_secret, ref_doctype, child_docname,
    company=None, configuration_name=None
):
    """
    Lightweight entry point.  Only lists remote names, splits into fixed-size
    chunks, and enqueues one background job per chunk.

    Kept fast so it can safely be called synchronously (e.g. from
    sync_data_from_remote) while still scaling to tens-of-thousands of records
    via background workers.

    Key design decisions
    --------------------
    - Two-phase fetch (names first, then full docs per chunk) prevents memory
      exhaustion on large datasets and keeps this request fast.
    - Fixed CHUNK_SIZE bounds each worker's runtime regardless of total volume.
    - Frappe commits automatically after a clean request or job return.
    """
    if not _acquire_sync_lock(child_docname):
        frappe.throw(
            _("A Journal Entry sync is already running for this row. "
              "Please wait for it to finish.")
        )

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
                _("Local DocType {0} is not installed on this site. "
                  "Run bench migrate and reload.").format(LOCAL_DT)
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
                "emkan_insights.emkan_insights.sync_journal_entry_from_remote"
                ".process_journal_entry_chunk",
                queue="long",
                timeout=1200,
                # Include timestamp to prevent DuplicateEntryError on the RQ Job doc
                # when the same child_docname triggers back-to-back syncs.
                job_name=f"je_sync_chunk_{child_docname}_{idx}_{int(time.time())}",
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
                f"Queued {len(chunks)} chunk(s) covering {len(names)} Journal Entries. "
                "Please wait for completion before starting again."
            ),
        }

    except Exception:
        _release_sync_lock(child_docname)
        raise


# ---------------------------------------------------------------------------
# BACKGROUND WORKER -- processes one chunk of Journal Entry names
# ---------------------------------------------------------------------------

@frappe.whitelist()
def process_journal_entry_chunk(
    site_url, api_key, api_secret, company, company_abbr,
    configuration_name, child_docname, remote_names, is_last_chunk=False,
    fetch_started_at=None
):
    """
    Background worker for a single fixed-size chunk of Journal Entry names.

    Per-chunk runtime is bounded by CHUNK_SIZE and the per-record HTTP
    timeout (60 s), so total worst-case time is predictable regardless of
    the total number of remote records.

    Child table rows (accounts)
    ---------------------------
    Each Journal Entry is fetched individually via
    /api/resource/Journal Entry/{name} which always returns the full document
    including all child-table rows.  This is the critical fix for the missing
    account rows observed when using the bulk list endpoint.

    _run_sync_loop -> _process_item purges any existing child rows for the
    External Journal Entry before inserting fresh ones from the remote data,
    preventing duplicate or orphaned account rows across re-syncs.

    The background job runner commits automatically on a clean return.
    """
    base_url = (site_url or "").rstrip("/")
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
    }
    state = {}

    try:
        # Phase 1: fetch full docs (with accounts child table) from remote
        docs = _fetch_full_docs_threaded(base_url, headers, remote_names)

        # Phase 2: normalise (prefix amended_from, etc.)
        docs = [
            _prepare_journal_entry_for_external_sync(item, company_abbr)
            for item in docs
        ]

        # Phase 3: save to External Journal Entry (child rows handled by
        # _process_item via _prepare_child_rows_for_sync)
        local_meta = frappe.get_meta(LOCAL_DT)
        local_fields = [df.fieldname for df in local_meta.fields]

        count, errors, not_saved = _run_sync_loop(
            docs, LOCAL_DT, local_fields, company, configuration_name, site_url
        )

        # Phase 4: single retry pass for transient failures
        if not_saved:
            retry_count, retry_errors, not_saved = _run_sync_loop(
                not_saved, LOCAL_DT, local_fields, company, configuration_name, site_url
            )
            count += retry_count
            errors.extend(retry_errors)

        state = _update_progress(child_docname, count, len(not_saved), errors)

        frappe.logger("external_sync").info(
            "JE chunk done: saved=%s, not_saved=%s, errors=%s",
            count, len(not_saved), len(errors),
        )

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
                "journal_entry_sync_complete",
                {
                    "child_docname": child_docname,
                    "count": state.get("count"),
                    "not_saved_count": state.get("not_saved_count"),
                    "errors": state.get("errors"),
                    "last_sync": last_sync,
                },
            )
