# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
import requests
from frappe import _

from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
    _get_company_abbr_for_sync,
    _fetch_remote_docs_with_children,
    _acquire_sync_lock,
    _release_sync_lock,
    _build_company_prefixed_remote_id,
)

CHUNK_SIZE = 300
FETCH_WORKERS = 8

REMOTE_DT = "Request for Quotation"
LOCAL_DT = "External Request for Quotation"
PROGRESS_KEY_PREFIX = "rfq_sync_progress"

# ✅ The PARENT doctype "External Request for Quotation" has a field called
# "remote_id" (confirmed from doctype meta dump), NOT "custom_remote_id".
# "custom_remote_id" only exists on the CHILD table "Request for Quotation Item".
PARENT_REMOTE_ID_FIELD = "remote_id"


def _fetch_remote_names(base_url, headers, remote_dt):
    url = f"{base_url}/api/resource/{remote_dt}"
    names = []
    limit = 1000
    start = 0

    while True:
        params = {
            "fields": json.dumps(["name"]),
            "filters": json.dumps([["docstatus", "in", [0, 1, 2]]]),
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


def _prepare_child_rows_for_sync(doc, table_fieldname, rows):
    table_df = doc.meta.get_field(table_fieldname)
    if not table_df or table_df.fieldtype != "Table" or not table_df.options:
        return rows

    child_meta = frappe.get_meta(table_df.options)
    child_fieldnames = {df.fieldname for df in child_meta.fields}

    cleaned_rows = []
    seen_row_ids = set()
    seen_row_fingerprints = set()

    for row in rows:
        if not isinstance(row, dict):
            cleaned_rows.append(row)
            continue

        row = dict(row)
        remote_child_row_id = row.get("name")
        if remote_child_row_id and remote_child_row_id in seen_row_ids:
            continue

        row.pop("name", None)
        row.pop("idx", None)
        row.pop("parent", None)
        row.pop("parenttype", None)
        row.pop("parentfield", None)
        row.pop("docstatus", None)
        row.pop("owner", None)
        row.pop("creation", None)
        row.pop("modified", None)
        row.pop("modified_by", None)

        if "custom_remote_id" in child_fieldnames and remote_child_row_id:
            row["custom_remote_id"] = remote_child_row_id

        fingerprint_row = dict(row)
        fingerprint_row.pop("custom_remote_id", None)
        row_fingerprint = json.dumps(fingerprint_row, sort_keys=True, default=str)
        if row_fingerprint in seen_row_fingerprints:
            continue

        if remote_child_row_id:
            seen_row_ids.add(remote_child_row_id)
        seen_row_fingerprints.add(row_fingerprint)
        cleaned_rows.append(row)

    return cleaned_rows


def _build_rfq_local_name(company_abbr, remote_id):
    """
    Build the local doc name directly from the company's abbreviation +
    the remote id — e.g. "IMC-RFQ-26-#####00016" (if the remote id already
    carries the company prefix, e.g. the remote site already names things
    "IMC-RFQ-26-#####00016") or "IMC-<remote_id>" (if it doesn't).

    This deliberately avoids Frappe's naming_series counter/autoname
    machinery entirely:
      - No dependency on the naming_series Select field's configured
        dropdown options (that was the actual bug — a fallback check
        against those options was silently reverting to the wrong,
        pre-existing series).
      - No shared counter to race against across parallel chunk workers.
      - Deterministic and idempotent: re-running the sync for the same
        remote_id always produces the same local name, so re-syncs
        naturally update the same row instead of creating duplicates.

    Same pattern as _build_company_prefixed_remote_id used for
    Material Request / the generic sync path.
    """
    return _build_company_prefixed_remote_id(company_abbr, remote_id)


def _dedupe_external_rfq_children(parent_name):
    """
    Remove duplicate item/supplier rows on External Request for Quotation.
    Same pattern as PI / RFQ target sync dedupe — keeps earliest row per key.
    """
    from frappe.utils import flt

    def _dedupe_table(doctype, parentfield, key_fields, float_fields=()):
        if not frappe.db.exists("DocType", doctype):
            return

        rows = frappe.db.get_all(
            doctype,
            filters={
                "parent": parent_name,
                "parenttype": LOCAL_DT,
                "parentfield": parentfield,
            },
            fields=["name"] + key_fields + list(float_fields),
            order_by="creation asc",
        )

        seen = set()
        delete_rows = []

        for row in rows:
            key = []
            for field in key_fields:
                key.append(str(row.get(field) or "").strip().lower())
            for field in float_fields:
                key.append(round(flt(row.get(field)), 3))
            key = tuple(key)

            if key in seen:
                delete_rows.append(row.name)
            else:
                seen.add(key)

        if delete_rows:
            frappe.db.delete(doctype, {"name": ["in", delete_rows]})

    _dedupe_table(
        "Request for Quotation Item",
        "items",
        [
            "item_code",
            "warehouse",
            "material_request",
            "material_request_item",
            "schedule_date",
            "uom",
        ],
        ["qty"],
    )

    _dedupe_table(
        "Request for Quotation Supplier",
        "suppliers",
        [
            "supplier",
            "contact",
            "email_id",
        ],
    )

    frappe.db.commit()


def _safe_dedupe_external_rfq(parent_name):
    """Never let a dedupe bug mark an otherwise-successful sync as failed."""
    try:
        _dedupe_external_rfq_children(parent_name)
    except Exception:
        frappe.log_error(
            title=f"External RFQ dedupe failed for {parent_name}",
            message=frappe.get_traceback(),
        )


def _process_rfq_item(local_dt, item, idx, local_fields, company, company_abbr, configuration_name, errors, site_url):
    """
    Custom _process_item for RFQ. Builds the local name directly from
    company_abbr + remote_id (see _build_rfq_local_name) instead of
    relying on naming_series autoname generation.
    """
    original_remote_id = item.get('name') or item.get('id')
    remote_docstatus = frappe.utils.cint(item.get("docstatus", 0))
    deferred_fields = {"amended_from"}

    if not original_remote_id:
        return False

    target_name = _build_rfq_local_name(company_abbr, original_remote_id)

    # ✅ Look up existing record by "remote_id" — the field that actually
    # exists on the parent doctype — and also by the deterministic target
    # name itself, so re-syncs always find and replace the same row.
    existing_name = None
    if "remote_id" in local_fields:
        existing_name = frappe.db.get_value(local_dt, {PARENT_REMOTE_ID_FIELD: original_remote_id}, "name")
    if not existing_name and frappe.db.exists(local_dt, target_name):
        existing_name = target_name

    # Purge parent + child rows keyed on BOTH looked-up name AND target name.
    # Clears orphan child rows left by a previous interrupted sync (parent gone
    # but items/suppliers survived), which otherwise causes 1× duplication per
    # re-fetch — same pattern as generic _process_item.
    purge_parent_names = {n for n in (existing_name, target_name) if n}
    for df in frappe.get_meta(local_dt).get_table_fields():
        for parent_name in purge_parent_names:
            frappe.db.delete(df.options, {
                "parent": parent_name,
                "parenttype": local_dt,
            })
    if existing_name:
        frappe.db.delete(local_dt, existing_name)
    frappe.db.commit()

    # ✅ Create new doc with an EXPLICIT deterministic name — bypasses
    # naming_series autoname generation entirely, so there's no dependency
    # on the Select field's configured dropdown options and no counter
    # race between parallel chunk workers.
    doc = frappe.new_doc(local_dt)
    doc.__islocal = True
    doc.name = target_name
    doc.flags.name_set = True

    # Keep naming_series populated as a plain display value (not used for
    # name generation) — dynamically built from the company abbreviation,
    # never hardcoded to one company and never taken from the remote's
    # own naming_series value (which was overriding ours previously).
    if "naming_series" in local_fields:
        doc.naming_series = f"{company_abbr}-RFQ-.YY.-"

    # Store remote_id for linking, on the PARENT field name that actually
    # exists: "remote_id", not "custom_remote_id".
    if "remote_id" in local_fields:
        doc.remote_id = original_remote_id

    # Map fields
    for field, value in item.items():
        if field in local_fields and field not in [
            'name', 'owner', 'creation', 'modified', 'naming_series', 'docstatus', 'remote_id'
        ] and field not in deferred_fields:
            if value is not None:
                if isinstance(value, list):
                    cleaned_rows = _prepare_child_rows_for_sync(doc, field, value)
                    for row_data in cleaned_rows:
                        doc.append(field, row_data)
                else:
                    doc.set(field, value)

    # Set company
    if 'company' in local_fields and company:
        doc.company = company

    # Set site_url
    for site_url_fieldname in ("source_site", "site_url"):
        if site_url_fieldname in local_fields:
            doc.set(site_url_fieldname, site_url)

    # Flags
    doc.flags.ignore_links = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_validate = True

    try:
        doc.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
        frappe.db.commit()

        # Set deferred fields
        for field in deferred_fields:
            value = item.get(field)
            if field in local_fields and value is not None:
                frappe.db.set_value(doc.doctype, doc.name, field, value, update_modified=False)

        # Sync docstatus
        if doc.docstatus != remote_docstatus:
            frappe.db.set_value(doc.doctype, doc.name, "docstatus", remote_docstatus, update_modified=False)

        frappe.db.commit()

        # PI-style safety net: strip any leftover duplicate child rows
        _safe_dedupe_external_rfq(doc.name)

        return True

    except Exception as e:
        frappe.db.rollback()
        errors.append({"remote_id": original_remote_id, "error": str(e)})
        return False


def _run_rfq_sync_loop(data, local_dt, local_fields, company, company_abbr, configuration_name, site_url):
    count = 0
    errors = []
    not_saved = []

    for idx, item in enumerate(data, start=1):
        result = _process_rfq_item(
            local_dt, item, idx, local_fields, company, company_abbr, configuration_name, errors, site_url
        )
        if result:
            count += 1
        else:
            not_saved.append(item)

    return count, errors, not_saved


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


def _get_progress_state(child_docname):
    """Read-only fetch of current progress state, used as a fallback when a
    chunk fails before it can call _update_progress itself."""
    raw = frappe.cache().get_value(_progress_key(child_docname))
    if raw:
        return json.loads(raw)
    return {
        "total_chunks": 0, "completed_chunks": 0, "count": 0,
        "not_saved_count": 0, "errors": [],
    }


@frappe.whitelist()
def sync_request_for_quotation_from_remote(site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None):
    if not _acquire_sync_lock(child_docname):
        frappe.throw(_("A Request for Quotation sync is already running for this row. Please wait for it to finish."))

    try:
        company_abbr = _get_company_abbr_for_sync(company)
        base_url = (site_url or "").rstrip("/")
        headers = {
            "Authorization": f"token {api_key}:{api_secret}",
            "Content-Type": "application/json",
        }

        if not frappe.db.exists("DocType", LOCAL_DT):
            frappe.throw(
                _("Local DocType {0} is not installed on this site. Run bench migrate and reload.")
                .format(LOCAL_DT)
            )

        names = _fetch_remote_names(base_url, headers, REMOTE_DT)

        if not names:
            _release_sync_lock(child_docname)
            last_sync = frappe.utils.now()
            frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
            frappe.db.commit()
            return {"count": 0, "fetched_total": 0, "not_saved_count": 0, "last_sync": last_sync, "errors": [], "missing_parent_accounts": []}

        chunks = [names[i:i + CHUNK_SIZE] for i in range(0, len(names), CHUNK_SIZE)]
        _init_progress(child_docname, len(chunks))

        for idx, chunk in enumerate(chunks):
            frappe.enqueue(
                "emkan_insights.emkan_insights.sync_request_for_quotation_from_remote.process_rfq_chunk",
                queue="long",
                timeout=900,
                job_name=f"rfq_sync_chunk_{child_docname}_{idx}",
                site_url=site_url,
                api_key=api_key,
                api_secret=api_secret,
                company=company,
                company_abbr=company_abbr,
                configuration_name=configuration_name,
                child_docname=child_docname,
                remote_names=chunk,
                is_last_chunk=(idx == len(chunks) - 1),
            )

        return f"Queued {len(chunks)} chunk(s) covering {len(names)} Request for Quotations"

    except Exception:
        _release_sync_lock(child_docname)
        raise


@frappe.whitelist()
def process_rfq_chunk(site_url, api_key, api_secret, company, company_abbr, configuration_name, child_docname, remote_names, is_last_chunk=False):
    base_url = (site_url or "").rstrip("/")
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
    }

    # Initialize `state` up front so that if the try block raises before
    # _update_progress ever runs, the finally block below has a safe
    # fallback instead of crashing with UnboundLocalError.
    state = None

    try:
        docs = _fetch_full_docs_threaded(base_url, headers, REMOTE_DT, remote_names)

        local_meta = frappe.get_meta(LOCAL_DT)
        local_fields = [df.fieldname for df in local_meta.fields]

        count, errors, not_saved = _run_rfq_sync_loop(
            docs, LOCAL_DT, local_fields, company, company_abbr, configuration_name, site_url
        )

        if not_saved:
            retry_count, retry_errors, not_saved = _run_rfq_sync_loop(
                not_saved, LOCAL_DT, local_fields, company, company_abbr, configuration_name, site_url
            )
            count += retry_count
            errors.extend(retry_errors)

        state = _update_progress(child_docname, count, len(not_saved), errors)

    except Exception:
        error_message = frappe.get_traceback()
        frappe.logger("external_sync").error(
            "RFQ chunk failed entirely for %s: %s", child_docname, error_message
        )
        state = _update_progress(
            child_docname, 0, len(remote_names),
            [{"remote_id": None, "error": f"Chunk failed: {error_message}"}]
        )
        raise

    finally:
        if is_last_chunk:
            if state is None:
                state = _get_progress_state(child_docname)

            last_sync = frappe.utils.now()
            frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
            frappe.db.commit()
            _release_sync_lock(child_docname)
            frappe.publish_realtime(
                "request_for_quotation_sync_complete",
                {
                    "child_docname": child_docname,
                    "count": state.get("count"),
                    "not_saved_count": state.get("not_saved_count"),
                    "errors": state.get("errors"),
                    "last_sync": last_sync,
                },
            )