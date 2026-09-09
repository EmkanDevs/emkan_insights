import frappe
import json
from frappe.utils import flt, getdate
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import set_source_site_fields

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_FIELDS = {
    "subscription_plans", "remote_id", "custom_remote_id",
    "naming_series", "name", "amended_from", "source_site", "custom_source_site"
}

LINK_DOCTYPES = {
    "Purchase Invoice", "Purchase Order", "Purchase Receipt",
    "Sales Invoice", "Sales Order", "Payment Request"
}

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


def resolve_local_reference(reference_doctype, reference_name, company_abbr=None):
    """Resolve external reference name to local with company abbreviation prefix."""
    if not reference_name or not reference_doctype:
        return None

    if reference_doctype not in LINK_DOCTYPES:
        return reference_name

    # 1. Check prefixed version first
    if company_abbr and not reference_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{reference_name}"
        if frappe.db.exists(reference_doctype, prefixed):
            return prefixed

    # 2. Check remote_id lookup
    meta = frappe.get_meta(reference_doctype)
    for field in ("remote_id", "custom_remote_id"):
        if meta.has_field(field):
            local_name = frappe.db.get_value(reference_doctype, {field: reference_name}, "name")
            if local_name:
                return local_name

    # 3. Check if raw name exists
    if frappe.db.exists(reference_doctype, reference_name):
        return reference_name

    return None


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_payment_request_docs(source_doctype, names, sync_now=False):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: _sync_payment_request_docs_job(
                source_doctype=source_doctype,
                names=chunk,
            ),
        )

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"prq_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_payment_request_sync._sync_payment_request_docs_job",
            queue="long",
            job_name=job_name,
            timeout=1000,
            source_doctype=source_doctype,
            names=chunk,
        )
        queued_chunks += 1

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def _sync_payment_request_docs_job(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            external = frappe.get_doc(source_doctype, name)
            sync_started_at = external.modified

            pr_name = sync_external_payment_request(source_doctype, name)
            results.append({"name": pr_name, "status": "success"})

            external.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )
        except Exception as e:
            log_record_failure(source_doctype, name, str(e))
            raise

    return results


# ==========================================================
# MAIN SYNC FUNCTION
# ==========================================================

def sync_external_payment_request(source_doctype, external_name):
    external = frappe.get_doc(source_doctype, external_name)

    remote_id = external.name
    company_abbr = frappe.db.get_value("Company", external.company, "abbr") or ""
    target_name = (
        f"{company_abbr}-{remote_id}"
        if company_abbr and not remote_id.startswith(f"{company_abbr}-")
        else remote_id
    )

    # Check existing by custom_remote_id
    existing_pr = frappe.db.get_value("Payment Request", {"custom_remote_id": remote_id}, "name")
    if existing_pr:
        return existing_pr

    if not getattr(external, "reference_doctype", None) or not getattr(external, "reference_name", None):
        frappe.throw("reference_doctype and reference_name are required")

    pr = frappe.new_doc("Payment Request")

    # Set name
    pr.name = target_name
    pr.flags.name_set = True

    # Set remote_id on correct field
    pr.custom_remote_id = remote_id

    set_source_site_fields(pr, site_url=getattr(external, "source_site", None))

    # Flags
    pr.flags.ignore_permissions = True
    pr.flags.ignore_naming_series = True

    # Naming series
    pr.naming_series = getattr(external, "naming_series", None) or "ACC-PRQ-.YYYY.-"

    # Copy fields
    for field, value in external.as_dict().items():
        if field in SYSTEM_FIELDS or field in IGNORE_FIELDS:
            continue
        if hasattr(pr, field):
            pr.set(field, value)

    # Resolve reference_name with company abbreviation
    ref_doctype = getattr(external, "reference_doctype", None)
    ref_name = getattr(external, "reference_name", None)
    
    if ref_doctype and ref_name:
        local_ref = resolve_local_reference(ref_doctype, ref_name, company_abbr)
        pr.reference_doctype = ref_doctype
        pr.reference_name = local_ref or ref_name

    # Set amounts
    pr.grand_total = flt(getattr(external, "grand_total", 0))
    pr.outstanding_amount = flt(getattr(external, "outstanding_amount", pr.grand_total))

    # Date
    if getattr(external, "transaction_date", None):
        pr.transaction_date = getdate(external.transaction_date)

    # Subscription plans child table
    pr.set("subscription_plans", [])
    for row in (external.get("subscription_plans") or []):
        pr.append("subscription_plans", {
            "plan": getattr(row, "plan", None),
            "qty": getattr(row, "qty", 1),
            "cost": getattr(row, "cost", 0),
        })

    # Insert
    pr.insert(
        ignore_permissions=True,
    )

    # Docstatus Sync
    if external.docstatus == 1:
        pr.submit()
    elif external.docstatus == 2:
        pr.submit()
        pr.cancel()

    # Update external with local name (update_modified=False keeps modified stable
    # so last_synced_on correctly suppresses re-sync on the next scheduler run).
    external.db_set("remote_id", pr.name, update_modified=False)

    return pr.name
