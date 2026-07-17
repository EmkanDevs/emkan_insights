import json

import frappe
import requests
from frappe import _

from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
    _build_company_prefixed_remote_id,
    _fetch_remote_docs_with_children,
    _get_company_abbr_for_sync,
    _run_sync_loop,
)


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

@frappe.whitelist()
def sync_leads_from_remote(site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None):
    """
    Synchronizes Leads from a remote site to 'External Lead' locally.
    Uses company abbreviation to prefix IDs, preventing cross-company overwrites.
    """
    frappe.logger("external_sync").info("Starting Lead sync with company %s", company)

    # 1. Get company abbreviation
    company_abbr = _get_company_abbr_for_sync(company)
    
    base_url = (site_url or "").rstrip('/')
    headers = {
        'Authorization': f'token {api_key}:{api_secret}',
        'Content-Type': 'application/json'
    }

    remote_dt = "Lead"
    local_dt = "External Lead"

    # 2. Check if local DocType exists
    if not frappe.db.exists("DocType", local_dt):
        frappe.throw(
            _("Local DocType {0} is not installed on this site. Run bench migrate and reload.")
            .format(local_dt)
        )

    remote_url = f"{base_url}/api/resource/{remote_dt}"
    data = []
    limit = 1000
    start = 0

    # 3. Fetch data from remote
    try:
        while True:
            params = {
                "fields": json.dumps(["*"]),
                "filters": json.dumps([["docstatus", "in", [0, 1, 2]]]),
                "limit_page_length": limit,
                "limit_start": start
            }
            response = requests.get(remote_url, headers=headers, params=params, timeout=120)
            response.raise_for_status()
            page = response.json().get('data', [])

            if not page:
                break

            data.extend(page)

            if len(page) < limit:
                break

            start += limit
    except Exception as e:
        frappe.throw(_("Lead sync failed: {0}").format(str(e)))

    # 4. Process and Prefix Data
    if data:
        # Lead usually doesn't have heavy child tables like Quotation, 
        # but we follow the pattern for consistency if needed.
        # If Lead has child tables (e.g., Notes), _fetch_remote_docs_with_children handles it.
        data = _fetch_remote_docs_with_children(base_url, headers, remote_dt, data)
        data = [_prepare_lead_for_external_sync(item, company_abbr) for item in data]

    local_meta = frappe.get_meta(local_dt)
    local_fields = [df.fieldname for df in local_meta.fields]

    # 5. Run Sync Loop
    count, errors, not_saved = _run_sync_loop(
        data, local_dt, local_fields, company, configuration_name, site_url
    )

    # 6. Retry if needed
    if not_saved:
        retry_count, retry_errors, not_saved = _run_sync_loop(
            not_saved, local_dt, local_fields, company, configuration_name, site_url
        )
        count += retry_count
        errors.extend(retry_errors)

    # 7. Update Last Sync
    last_sync = frappe.utils.now()
    frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
    frappe.db.commit()

    return {
        "count": count,
        "fetched_total": len(data),
        "not_saved_count": len(not_saved),
        "last_sync": last_sync,
        "errors": errors,
        "missing_parent_accounts": not_saved,
    }
