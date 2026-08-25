import frappe
import time
from contextlib import contextmanager
from inspect import signature
from frappe.model.document import Document

from emkan_insights.emkan_insights.registry.fetch_registry import (
    SPECIAL_FETCHERS,
    DEFAULT_FETCHER,
)

from emkan_insights.emkan_insights.registry.sync_registry import (
    SYNC_METHODS,
)

SYNC_SEQUENCE = [
    "UOM",
    "Account",
    "Cost Center",
    "Tax Category",
    "Item Tax Template",
    "Mode of Payment",
    "Warehouse",
    "Manufacturer",
    "Payment Term",
    "Payment Terms Template",
    "Price List",
    "Supplier Group",
    "Customer Group",
    "Department",
    "Designation",
    "Employee",
    "Sales Person",
    "Territory",
    "Bank",
    "Bank Account",
    "Location",
    "Project Type",
    "Asset Category",
    "Sales Stage",
    "Lead Source",
    "Letter Head",
    "Terms and Conditions",
    "Address",
    "Contact",
    "Supplier",
    "Item Group",
    "Customer",
    "Contract",
    "Email Template",
    "Item",
    "Asset",
    "Project",
    "Purchase Taxes and Charges Template",
    "Lead",
    "Sales Taxes and Charges Template",
    "Prospect",
    "Opportunity",
    "BOM",
    "Production Plan",
    "Work Order",
    "Material Request",
    "Stock Entry",
    "Request for Quotation",
    "Supplier Quotation",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Quotation",
    "Sales Order",
    "Delivery Note",
    "Sales Invoice",
    "Expense Claim",
    "Payment Entry",
    "Journal Entry",
    "Stock Reconciliation",
]


SYNC_SEQUENCE_INDEX = {doctype: idx for idx, doctype in enumerate(SYNC_SEQUENCE)}
GENERIC_INLINE_SYNC_REFS = {
    "Asset Category",
    "Bank",
    "Bank Account",
    "Contract",
    "Customer",
    "Customer Group",
    "Item Group",
    "Location",
    "Manufacturer",
    "Price List",
    "Project Type",
    "Supplier",
    "Supplier Group",
    "Territory",
    "UOM",
    "Warehouse",
}
SEQUENCE_VALIDATION_IGNORE_LINKS = {
    ("Contact", "links.link_name"),
    ("Address", "links.link_name"),
    ("Customer", "lead_name"),
    ("Customer", "opportunity_name"),
    ("Customer", "prospect_name"),
    ("Sales Person", "targets.item_group"),
    ("Territory", "targets.item_group"),
    ("Item", "default_bom"),
    ("Asset", "purchase_receipt"),
    ("Asset", "purchase_invoice"),
    ("Asset", "journal_entry"),
    ("Asset", "journal_entry_for_scrap"),
    ("Project", "sales_order"),
    ("Prospect", "opportunities.opportunity"),
    ("Production Plan", "sales_orders.sales_order"),
    ("Production Plan", "material_requests.material_request"),
    ("Production Plan", "po_items.sales_order"),
    ("Production Plan", "po_items.material_request"),
    ("Production Plan", "mr_items.sales_order"),
    ("Production Plan", "prod_plan_references.sales_order"),
    ("Production Plan", "sub_assembly_items.purchase_order"),
    ("Production Plan", "sub_assembly_items.sales_order"),
    ("Work Order", "material_request"),
    ("Work Order", "sales_order"),
    ("Material Request", "items.sales_order"),
    ("Stock Entry", "purchase_order"),
    ("Stock Entry", "purchase_receipt_no"),
    ("Stock Entry", "delivery_note_no"),
    ("Stock Entry", "sales_invoice_no"),
    ("Stock Entry", "items.reference_purchase_receipt"),
    ("Stock Entry", "subcontracting_order"),
    ("Stock Entry", "credit_note"),
    ("Supplier Quotation", "items.sales_order"),
    ("Purchase Order", "inter_company_order_reference"),
    ("Purchase Order", "items.sales_order"),
    ("Purchase Order", "items.sales_order_item"),
    ("Purchase Receipt", "items.purchase_invoice"),
    ("Purchase Receipt", "inter_company_reference"),
    ("Purchase Receipt", "items.sales_order"),
    ("Purchase Receipt", "items.sales_order_item"),
    ("Purchase Invoice", "inter_company_invoice_reference"),
    ("Delivery Note", "items.against_sales_invoice"),
}
SITE_PIPELINE_LOCK_TTL = 12 * 60 * 60
ROW_WAIT_TIMEOUT = 4 * 60 * 60
ROW_WAIT_INTERVAL = 15
LINK_AUDIT_MAX_DOCS_PER_DOCTYPE = 500


def _log_sync_event(title, message=None):
    frappe.log_error(
        title=title[:140],
        message=message or title,
    )


def _log_sync_failure(title):
    frappe.log_error(
        title=title[:140],
        message=frappe.get_traceback(),
    )


def _site_pipeline_lock_key(config_name):
    return f"external_site_pipeline_lock:{config_name}"


def _row_fetch_lock_key(child_docname):
    return f"external_sync_lock:{child_docname}"


def _target_sync_lock_key(external_doctype):
    return f"external_target_sync_lock:{external_doctype}"


def _acquire_site_pipeline_lock(config_name):
    return bool(
        frappe.cache().set(
            _site_pipeline_lock_key(config_name),
            "1",
            nx=True,
            ex=SITE_PIPELINE_LOCK_TTL,
        )
    )


def _release_site_pipeline_lock(config_name):
    frappe.cache().delete(_site_pipeline_lock_key(config_name))


def _wait_for_redis_lock_to_clear(lock_key, timeout=ROW_WAIT_TIMEOUT):
    started_at = time.monotonic()
    while frappe.cache().execute_command("EXISTS", lock_key):
        if time.monotonic() - started_at > timeout:
            frappe.throw(f"Timed out waiting for sync lock to clear: {lock_key}")
        time.sleep(ROW_WAIT_INTERVAL)


def _ordered_fetch_rows(config):
    rows = list(config.fetch_data or [])
    return sorted(
        rows,
        key=lambda row: (
            SYNC_SEQUENCE_INDEX.get(row.ref_doctype, len(SYNC_SEQUENCE)),
            row.idx or 0,
            row.ref_doctype or "",
        ),
    )


def _external_doctype_for_ref(ref_doctype):
    if ref_doctype == "UOM":
        return "External Uom"
    return f"External {ref_doctype}"


def _ref_doctype_for_link(doctype):
    if doctype == "External Uom":
        return "UOM"
    if doctype.startswith("External "):
        return doctype.replace("External ", "", 1)
    return doctype


def _sequence_link_dependencies(ref_doctype):
    external_doctype = _external_doctype_for_ref(ref_doctype)
    if not frappe.db.exists("DocType", external_doctype):
        return []

    dependencies = []
    meta = frappe.get_meta(external_doctype)
    for field in meta.fields:
        dependencies.extend(_field_link_dependencies(ref_doctype, field.fieldname, field))
        if field.fieldtype == "Table" and field.options and frappe.db.exists("DocType", field.options):
            for child_field in frappe.get_meta(field.options).fields:
                dependencies.extend(
                    _field_link_dependencies(
                        ref_doctype,
                        f"{field.fieldname}.{child_field.fieldname}",
                        child_field,
                    )
                )
    return dependencies


def _field_link_dependencies(ref_doctype, field_path, field):
    if field.fieldtype != "Link" or not field.options:
        return []

    dependency_ref = _ref_doctype_for_link(field.options)
    if dependency_ref == ref_doctype or dependency_ref not in SYNC_SEQUENCE_INDEX:
        return []

    return [
        {
            "field": field_path,
            "depends_on": dependency_ref,
            "link_doctype": field.options,
            "required": bool(field.reqd),
        }
    ]


@frappe.whitelist()
def validate_sync_sequence_link_dependencies(include_ignored=0):
    include_ignored = bool(int(include_ignored))
    violations = []
    bypassed = []

    for ref_doctype in SYNC_SEQUENCE:
        for dependency in _sequence_link_dependencies(ref_doctype):
            dependency_index = SYNC_SEQUENCE_INDEX[dependency["depends_on"]]
            current_index = SYNC_SEQUENCE_INDEX[ref_doctype]
            if dependency_index < current_index:
                continue

            row = {
                "doctype": ref_doctype,
                "field": dependency["field"],
                "depends_on": dependency["depends_on"],
                "link_doctype": dependency["link_doctype"],
                "required": dependency["required"],
                "doctype_position": current_index,
                "dependency_position": dependency_index,
            }
            if (ref_doctype, dependency["field"]) in SEQUENCE_VALIDATION_IGNORE_LINKS:
                bypassed.append(row)
            else:
                violations.append(row)

    result = {
        "status": "pass" if not violations else "fail",
        "violations": violations,
        "violation_count": len(violations),
        "bypassed_count": len(bypassed),
    }
    if include_ignored:
        result["bypassed"] = bypassed
    return result


def _call_sync_method(sync_method, **kwargs):
    accepted_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in signature(sync_method).parameters
    }
    return sync_method(**accepted_kwargs)


@contextmanager
def _link_validation_disabled():
    original_validate_links = Document._validate_links
    Document._validate_links = lambda self: None
    try:
        yield
    finally:
        Document._validate_links = original_validate_links


def _sync_inline_generic_records(ref_doctype, external_doctype, names):
    from emkan_insights.emkan_insights.external_sync import (
        _sync_external_docs_worker,
        get_external_sync_info,
    )

    info = get_external_sync_info(external_doctype)
    if not info:
        frappe.throw(f"Target doctype could not be determined for {external_doctype}")

    return _sync_external_docs_worker(
        source_doctype=external_doctype,
        target_doctype=info["target"],
        names=names,
        title_field_source=info["title_field"],
        mandatory_defaults=info["defaults"],
        raise_on_error=True,
    )


def _sync_records_for_sequence_row(sync_method, row, external_doctype, names, config):
    if row.ref_doctype in GENERIC_INLINE_SYNC_REFS:
        return _sync_inline_generic_records(row.ref_doctype, external_doctype, names)

    result = _call_sync_method(
        sync_method,
        names=names,
        source_doctype=external_doctype,
        company=config.company,
        sync_now=True,
    )
    _throw_if_sync_result_failed(row.ref_doctype, result)
    return result


def _throw_if_sync_result_failed(ref_doctype, result):
    if isinstance(result, list):
        failed = [
            row for row in result
            if isinstance(row, dict) and str(row.get("status") or "").lower() in {"failed", "error"}
        ]
        if failed:
            frappe.throw(f"Failed to sync {ref_doctype}: {len(failed)} failed record(s)")
        return

    if not isinstance(result, dict):
        return

    failed_count = frappe.utils.cint(result.get("failed_count") or result.get("failed"))
    if failed_count:
        frappe.throw(f"Failed to sync {ref_doctype}: {failed_count} failed record(s)")

    status = str(result.get("status") or "").lower()
    if status in {"failed", "error"}:
        frappe.throw(f"Failed to sync {ref_doctype}: {result}")


def _target_doctype_for_external_doctype(external_doctype):
    from emkan_insights.emkan_insights.external_sync import get_external_sync_info

    info = get_external_sync_info(external_doctype)
    if info:
        return info["target"]

    if external_doctype.startswith("External "):
        target = external_doctype.replace("External ", "", 1)
        if target == "Assets":
            return "Asset"
        if target == "Uom":
            return "UOM"
        return target

    return None


def _target_names_for_external_docs(external_doctype, target_doctype, config):
    external_names = get_external_doc_names_for_config(external_doctype, config)
    if not external_names:
        return []

    candidates = set()
    external_meta = frappe.get_meta(external_doctype)
    target_meta = frappe.get_meta(target_doctype)

    for row in frappe.get_all(
        external_doctype,
        filters={"name": ["in", external_names]},
        fields=["name", "remote_id"] if external_meta.has_field("remote_id") else ["name"],
        limit_page_length=LINK_AUDIT_MAX_DOCS_PER_DOCTYPE,
    ):
        candidates.add(row.name)
        if row.get("remote_id"):
            candidates.add(row.remote_id)

    filters = [["name", "in", list(candidates)]]
    if target_meta.has_field("remote_id"):
        filters.append(["remote_id", "in", list(candidates)])
    if target_meta.has_field("custom_remote_id"):
        filters.append(["custom_remote_id", "in", list(candidates)])

    target_names = set()
    for fieldname, operator, values in filters:
        if not values:
            continue
        target_names.update(
            frappe.get_all(
                target_doctype,
                filters={fieldname: [operator, values]},
                pluck="name",
            )
        )

    return sorted(target_names)


def _invalid_link_rows(doc):
    invalid_links, cancelled_links = doc.get_invalid_links()
    for child in doc.get_all_children():
        child_invalid, child_cancelled = child.get_invalid_links(
            is_submittable=doc.meta.is_submittable
        )
        invalid_links.extend(child_invalid)
        cancelled_links.extend(child_cancelled)

    rows = []
    for doctype, docname, message in invalid_links:
        rows.append({"type": "missing", "link_doctype": doctype, "link_name": docname, "message": message})
    for doctype, docname, message in cancelled_links:
        rows.append({"type": "cancelled", "link_doctype": doctype, "link_name": docname, "message": message})
    return rows


@frappe.whitelist()
def validate_external_site_configuration_target_links(site_config):
    config = frappe.get_doc("External Site Configuration", site_config)
    mismatches = []
    checked = 0
    capped_doctypes = []

    for row in _ordered_fetch_rows(config):
        external_doctype = _external_doctype_for_ref(row.ref_doctype)
        if not frappe.db.exists("DocType", external_doctype):
            continue

        target_doctype = _target_doctype_for_external_doctype(external_doctype)
        if not target_doctype or not frappe.db.exists("DocType", target_doctype):
            continue

        target_names = _target_names_for_external_docs(external_doctype, target_doctype, config)
        if len(target_names) >= LINK_AUDIT_MAX_DOCS_PER_DOCTYPE:
            capped_doctypes.append(target_doctype)

        for target_name in target_names:
            doc = frappe.get_doc(target_doctype, target_name)
            checked += 1
            link_rows = _invalid_link_rows(doc)
            if link_rows:
                mismatches.append(
                    {
                        "doctype": target_doctype,
                        "name": target_name,
                        "source_doctype": external_doctype,
                        "links": link_rows,
                    }
                )

    result = {
        "status": "pass" if not mismatches else "mismatch",
        "checked": checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
        "truncated_mismatches": max(len(mismatches) - 100, 0),
        "capped_doctypes": capped_doctypes,
    }
    _log_sync_event(
        "Sequential Link Validation Completed",
        frappe.as_json(result, indent=2)[:10000],
    )
    return result


def _source_site_filters(config):
    values = {
        config.name,
        (config.site_url or "").rstrip("/"),
        config.site_url,
    }
    return [value for value in values if value]


def _external_doc_filters(external_doctype, config):
    meta = frappe.get_meta(external_doctype)
    filters = []

    if meta.has_field("source_site"):
        filters.append({"source_site": ["in", _source_site_filters(config)]})

    if meta.has_field("company") and config.company:
        filters.append({"company": config.company})

    return filters


def _delete_in_batches(doctype, names, batch_size=500):
    if not names:
        return 0

    deleted = 0
    for start in range(0, len(names), batch_size):
        batch = names[start:start + batch_size]
        frappe.db.delete(doctype, {"name": ["in", batch]})
        deleted += len(batch)
    return deleted


def _external_doc_where_clause(external_doctype, config):
    meta = frappe.get_meta(external_doctype)
    clauses = []
    values = []

    if meta.has_field("source_site"):
        source_values = _source_site_filters(config)
        if source_values:
            clauses.append(f"`tab{external_doctype}`.`source_site` in %s")
            values.append(tuple(source_values))

    if meta.has_field("company") and config.company:
        clauses.append(f"`tab{external_doctype}`.`company` = %s")
        values.append(config.company)

    if not clauses:
        return None, None

    return " OR ".join(f"({clause})" for clause in clauses), values


def _count_external_docs_for_config(external_doctype, config):
    where_clause, values = _external_doc_where_clause(external_doctype, config)
    if not where_clause:
        return 0

    return frappe.db.sql(
        f"select count(*) from `tab{external_doctype}` where {where_clause}",
        values,
    )[0][0]


def _delete_external_docs_for_config_fast(external_doctype, config, dry_run=True):
    count = _count_external_docs_for_config(external_doctype, config)
    if dry_run or not count:
        return count

    where_clause, values = _external_doc_where_clause(external_doctype, config)
    meta = frappe.get_meta(external_doctype)

    for table_field in meta.get_table_fields():
        if frappe.db.exists("DocType", table_field.options):
            frappe.db.sql(
                f"""
                delete from `tab{table_field.options}`
                where parenttype = %s
                    and parent in (
                        select name from `tab{external_doctype}`
                        where {where_clause}
                    )
                """,
                [external_doctype] + values,
            )

    frappe.db.sql(
        f"delete from `tab{external_doctype}` where {where_clause}",
        values,
    )

    return count


def _delete_external_docs_for_config(external_doctype, config, dry_run=True, fast=False):
    if fast:
        return _delete_external_docs_for_config_fast(
            external_doctype,
            config,
            dry_run=dry_run,
        )

    names = set()
    for filters in _external_doc_filters(external_doctype, config):
        names.update(frappe.get_all(external_doctype, filters=filters, pluck="name"))

    meta = frappe.get_meta(external_doctype)
    if meta.has_field("lft") and meta.has_field("rgt") and names:
        ordered = frappe.get_all(
            external_doctype,
            filters={"name": ["in", list(names)]},
            fields=["name"],
            order_by="rgt asc",
        )
        names = [row.name for row in ordered]
    else:
        names = sorted(names)

    if not dry_run:
        if fast:
            table_fields = meta.get_table_fields()
            for table_field in table_fields:
                if frappe.db.exists("DocType", table_field.options):
                    _delete_in_batches(table_field.options, names)
            _delete_in_batches(external_doctype, names)
        else:
            for name in names:
                frappe.delete_doc(
                    external_doctype,
                    name,
                    force=True,
                    ignore_permissions=True,
                    ignore_missing=True,
                )

    return len(names)


@frappe.whitelist()
def clean_external_site_configuration_staging(site_config, dry_run=1, confirm=0, fast=0):
    """
    Delete fetched External * staging rows for one External Site Configuration.

    This intentionally does not delete ERPNext target documents. Submitted
    accounting/stock docs require a separate, explicit cancellation/deletion
    plan because they produce GL and Stock Ledger side effects.
    """
    dry_run = bool(int(dry_run))
    confirm = bool(int(confirm))
    fast = bool(int(fast))

    if not dry_run and not confirm:
        frappe.throw("Pass confirm=1 to delete CPC external staging data.")

    config = frappe.get_doc("External Site Configuration", site_config)
    deleted_by_doctype = {}

    for row in _ordered_fetch_rows(config):
        external_doctype = _external_doctype_for_ref(row.ref_doctype)
        if not frappe.db.exists("DocType", external_doctype):
            continue

        count = _delete_external_docs_for_config(
            external_doctype,
            config,
            dry_run=dry_run,
            fast=fast,
        )
        if count:
            deleted_by_doctype[external_doctype] = count
            if not dry_run and fast:
                frappe.db.commit()

    reset_fetch_rows = len(config.fetch_data or [])
    if not dry_run:
        for row in config.fetch_data or []:
            frappe.db.set_value(
                "External Site Configuration CT",
                row.name,
                "last_sync",
                None,
                update_modified=False,
            )
        frappe.db.commit()

    return {
        "status": "dry_run" if dry_run else "deleted",
        "site_config": config.name,
        "company": config.company,
        "external_rows": deleted_by_doctype,
        "external_rows_total": sum(deleted_by_doctype.values()),
        "fetch_rows_to_reset": reset_fetch_rows,
        "target_docs_deleted": 0,
        "fast": fast,
        "note": "Only External * staging rows are included; ERPNext target docs are not deleted.",
    }


@frappe.whitelist()
def enqueue_clean_external_site_configuration_staging(site_config, confirm=0, fast=1):
    confirm = bool(int(confirm))
    fast = bool(int(fast))
    if not confirm:
        frappe.throw("Pass confirm=1 to enqueue CPC external staging cleanup.")

    job_name = f"external_site_staging_cleanup_{site_config}_{frappe.generate_hash(length=8)}"
    frappe.enqueue(
        "emkan_insights.emkan_insights.scheduler.clean_external_site_configuration_staging",
        queue="long",
        timeout=SITE_PIPELINE_LOCK_TTL,
        job_name=job_name,
        site_config=site_config,
        dry_run=0,
        confirm=1,
        fast=1 if fast else 0,
    )

    return {
        "status": "queued",
        "site_config": site_config,
        "job_name": job_name,
        "fast": fast,
    }


@frappe.whitelist()
def fetch_external_site_configuration_sequence(site_config):
    frappe.enqueue(
        "emkan_insights.emkan_insights.scheduler.fetch_external_site_configuration_sequence_job",
        queue="long",
        timeout=SITE_PIPELINE_LOCK_TTL,
        job_name=f"external_site_fetch_{site_config}_{frappe.generate_hash(length=8)}",
        site_config=site_config,
    )
    return {
        "status": "queued",
        "message": f"Sequential fetch queued for {site_config}",
    }


@frappe.whitelist()
def sync_external_site_configuration_sequence(site_config):
    frappe.enqueue(
        "emkan_insights.emkan_insights.scheduler.sync_external_site_configuration_sequence_job",
        queue="long",
        timeout=SITE_PIPELINE_LOCK_TTL,
        job_name=f"external_site_sync_{site_config}_{frappe.generate_hash(length=8)}",
        site_config=site_config,
    )
    return {
        "status": "queued",
        "message": f"Sequential sync queued for {site_config}",
    }


@frappe.whitelist()
def fetch_and_sync_external_site_configuration_sequence(site_config):
    frappe.enqueue(
        "emkan_insights.emkan_insights.scheduler.run_external_site_configuration_sequence_job",
        queue="long",
        timeout=SITE_PIPELINE_LOCK_TTL,
        job_name=f"external_site_fetch_sync_{site_config}_{frappe.generate_hash(length=8)}",
        site_config=site_config,
    )
    return {
        "status": "queued",
        "message": f"Sequential fetch and sync queued for {site_config}",
    }


@frappe.whitelist()
def run_external_site_configuration_sequence(site_config):
    return fetch_and_sync_external_site_configuration_sequence(site_config)


@frappe.whitelist()
def stop_external_site_configuration_jobs(site_config):
    from rq import cancel_job
    from rq.command import send_stop_job_command
    from rq.exceptions import InvalidJobOperation, NoSuchJobError

    from frappe.core.doctype.rq_job.rq_job import RQJob
    from frappe.utils.background_jobs import get_redis_conn

    conn = get_redis_conn()
    stopped_job_ids = []
    matching_job_ids = RQJob.get_matching_job_ids(
        {
            "filters": {
                "status": ["in", ["queued", "started"]],
                "queue": ["in", ["long", "default", "short"]],
            }
        }
    )

    for job_id in matching_job_ids:
        job = frappe.get_doc("RQ Job", job_id)
        job_name = job.job_name or ""
        arguments = job.arguments or ""
        description = getattr(job.job, "description", "") or ""

        if "emkan_insights" not in job_name and "emkan_insights" not in description:
            continue
        if (
            site_config not in job_name
            and f'"site_config": "{site_config}"' not in arguments
            and f"'site_config': '{site_config}'" not in description
            and f'"configuration_name": "{site_config}"' not in arguments
            and f"'configuration_name': '{site_config}'" not in description
        ):
            continue

        if job.status == "started":
            try:
                send_stop_job_command(connection=conn, job_id=job_id)
            except (InvalidJobOperation, NoSuchJobError):
                pass
        else:
            cancel_job(job_id, connection=conn)

        try:
            job.job.delete()
        except Exception:
            pass
        stopped_job_ids.append(job_id)

    lock_keys = [
        _site_pipeline_lock_key(site_config),
    ]
    lock_keys.extend(frappe.cache().execute_command("KEYS", "external_sync_lock:*") or [])
    lock_keys.extend(frappe.cache().execute_command("KEYS", "external_target_sync_lock:*") or [])

    deleted_locks = 0
    if lock_keys:
        deleted_locks = frappe.cache().execute_command("DEL", *lock_keys)

    return {
        "status": "stopped",
        "stopped_jobs": len(stopped_job_ids),
        "deleted_locks": deleted_locks,
    }


@frappe.whitelist()
def stop_external_sync_jobs(method_contains="emkan_insights.emkan_insights.scheduler.sync_external_docs_scheduler"):
    from rq import cancel_job
    from rq.command import send_stop_job_command
    from rq.exceptions import InvalidJobOperation, NoSuchJobError

    from frappe.core.doctype.rq_job.rq_job import RQJob
    from frappe.utils.background_jobs import get_redis_conn

    conn = get_redis_conn()
    stopped_job_ids = []
    matching_job_ids = RQJob.get_matching_job_ids(
        {
            "filters": {
                "status": ["in", ["queued", "started"]],
                "queue": ["in", ["long", "default", "short"]],
            }
        }
    )

    for job_id in matching_job_ids:
        job = frappe.get_doc("RQ Job", job_id)
        job_name = job.job_name or ""
        arguments = job.arguments or ""
        description = getattr(job.job, "description", "") or ""

        if (
            method_contains not in job_name
            and method_contains not in arguments
            and method_contains not in description
        ):
            continue

        if job.status == "started":
            try:
                send_stop_job_command(connection=conn, job_id=job_id)
            except (InvalidJobOperation, NoSuchJobError):
                pass
        else:
            cancel_job(job_id, connection=conn)

        try:
            job.job.delete()
        except Exception:
            pass
        stopped_job_ids.append(job_id)

    return {
        "status": "stopped",
        "method_contains": method_contains,
        "stopped_jobs": len(stopped_job_ids),
        "job_ids": stopped_job_ids,
    }


def fetch_external_site_configuration_sequence_job(site_config):
    if not _acquire_site_pipeline_lock(site_config):
        _log_sync_event(
            "Sequential Fetch Skipped",
            f"External Site Configuration {site_config} is already syncing.",
        )
        return {
            "status": "skipped",
            "message": f"Sequential fetch is already running for {site_config}",
        }

    try:
        _fetch_configuration_sequentially(site_config)
        return {
            "status": "complete",
            "message": f"Sequential fetch completed for {site_config}",
        }
    finally:
        _release_site_pipeline_lock(site_config)


def sync_external_site_configuration_sequence_job(site_config):
    if not _acquire_site_pipeline_lock(site_config):
        _log_sync_event(
            "Sequential Sync Skipped",
            f"External Site Configuration {site_config} is already syncing.",
        )
        return {
            "status": "skipped",
            "message": f"Sequential sync is already running for {site_config}",
        }

    try:
        _sync_configuration_sequentially(site_config)
        link_validation = validate_external_site_configuration_target_links(site_config)
        status = "complete" if link_validation["status"] == "pass" else "complete_with_link_mismatches"
        return {
            "status": status,
            "message": f"Sequential sync completed for {site_config}",
            "link_validation": link_validation,
        }
    finally:
        _release_site_pipeline_lock(site_config)


def run_external_site_configuration_sequence_job(site_config):
    if not _acquire_site_pipeline_lock(site_config):
        _log_sync_event(
            "Sequential Fetch And Sync Skipped",
            f"External Site Configuration {site_config} is already syncing.",
        )
        return {
            "status": "skipped",
            "message": f"Sequential fetch and sync is already running for {site_config}",
        }

    try:
        _fetch_configuration_sequentially(site_config)
        _sync_configuration_sequentially(site_config)
        link_validation = validate_external_site_configuration_target_links(site_config)
        status = "complete" if link_validation["status"] == "pass" else "complete_with_link_mismatches"
        return {
            "status": status,
            "message": f"Sequential fetch and sync completed for {site_config}",
            "link_validation": link_validation,
        }
    finally:
        _release_site_pipeline_lock(site_config)


def _fetch_configuration_sequentially(config_name):
    config = frappe.get_doc(
        "External Site Configuration",
        config_name,
    )

    _log_sync_event("Sequential Fetch Started", f"Processing {config.name}")

    for row in _ordered_fetch_rows(config):
        _log_sync_event(
            f"Fetching {row.ref_doctype}",
            f"Fetching {row.ref_doctype} from External Site Configuration {config.name}",
        )
        _wait_for_redis_lock_to_clear(_row_fetch_lock_key(row.name))

        fetch_method = SPECIAL_FETCHERS.get(
            row.ref_doctype,
            DEFAULT_FETCHER,
        )

        try:
            fetch_method(
                site_url=config.site_url,
                api_key=config.api_key,
                api_secret=config.api_secret,
                ref_doctype=row.ref_doctype,
                child_docname=row.name,
                company=config.company,
                configuration_name=config.name,
            )
            _wait_for_redis_lock_to_clear(_row_fetch_lock_key(row.name))

        except Exception:
            _log_sync_failure(f"Failed to fetch {row.ref_doctype} from {config.name}")
            raise

    _log_sync_event(
        "Sequential Fetch Completed",
        f"Completed sequential fetch for {config.name}",
    )


def _sync_configuration_sequentially(config_name):
    config = frappe.get_doc(
        "External Site Configuration",
        config_name,
    )

    for row in _ordered_fetch_rows(config):

        sync_method = SYNC_METHODS.get(row.ref_doctype)

        if not sync_method:
            _log_sync_event(
                "Sequential Sync Skipped",
                f"No sync method configured for '{row.ref_doctype}'"
            )
            continue

        external_doctype = _external_doctype_for_ref(row.ref_doctype)

        if not frappe.db.exists("DocType", external_doctype):
            continue

        _log_sync_event(
            title="Sync Debug",
            message=f"Looking for pending {external_doctype} records"
        )

        names = get_external_doc_names_to_sync(
            external_doctype,
            config=config,
        )

        if not names:
            _log_sync_event(
                f"No {row.ref_doctype} Records Pending",
                f"No {row.ref_doctype} records pending sync."
            )
            continue

        _log_sync_event(
            f"Syncing {row.ref_doctype}",
            f"Syncing {len(names)} {row.ref_doctype} record(s)."
        )

        try:
            _wait_for_redis_lock_to_clear(_target_sync_lock_key(external_doctype))
            with _link_validation_disabled():
                _sync_records_for_sequence_row(
                    sync_method,
                    row=row,
                    external_doctype=external_doctype,
                    names=names,
                    config=config,
                )
            _wait_for_redis_lock_to_clear(_target_sync_lock_key(external_doctype))

        except Exception:
            _log_sync_failure(f"Failed to sync {row.ref_doctype}")
            raise


def fetch_scheduler(site_config=None):
    try:
        filters = {}
        if site_config:
            filters["name"] = site_config
        configs = frappe.get_all(
            "External Site Configuration",
            filters=filters,
            pluck="name",
        )
        _log_sync_event("Fetch Scheduler Configs", f"Configs: {configs}")

        for config_name in configs:
            if not _acquire_site_pipeline_lock(config_name):
                _log_sync_event(
                    "Sequential Sync Skipped",
                    f"External Site Configuration {config_name} is already syncing.",
                )
                continue

            try:
                _fetch_configuration_sequentially(config_name)
            finally:
                _release_site_pipeline_lock(config_name)

        _log_sync_event(
            "Fetch Scheduler Completed",
            "All fetches completed. Fetch phase enqueued; sync will run in the next scheduled job."
        )
    except Exception:
        _log_sync_failure("fetch_scheduler failed")
        raise


def sync_external_docs_scheduler(site_config=None):
    """
    Separate scheduled job (runs ~30 min after fetch_scheduler).

    fetch_scheduler only *enqueues* the remote API fetch via frappe.enqueue().
    By the time this function runs, the background worker has finished writing
    External Warehouse rows to the DB, so get_external_doc_names_to_sync()
    will find records ready to sync.
    """

    filters = {}
    if site_config:
        filters["name"] = site_config

    configs = frappe.get_all(
        "External Site Configuration",
        filters=filters,
        pluck="name",
    )

    for config_name in configs:
        if not _acquire_site_pipeline_lock(config_name):
            _log_sync_event(
                "Sequential Target Sync Skipped",
                f"External Site Configuration {config_name} is already syncing.",
            )
            continue

        try:
            _sync_configuration_sequentially(config_name)
        finally:
            _release_site_pipeline_lock(config_name)


def _external_doc_sync_filters(external_doctype, config=None):
    meta = frappe.get_meta(external_doctype)
    clauses = []
    values = []

    if not config:
        return clauses, values

    if meta.has_field("source_site"):
        source_values = _source_site_filters(config)
        if source_values:
            clauses.append(f"`tab{external_doctype}`.`source_site` in %s")
            values.append(tuple(source_values))

    if meta.has_field("company") and config.company:
        clauses.append(f"`tab{external_doctype}`.`company` = %s")
        values.append(config.company)

    return clauses, values


def get_external_doc_names_to_sync(external_doctype, config=None):
    """
    Returns all External document names that require syncing.

    A document requires syncing when:
    - last_synced_on is empty
    OR
    - modified > last_synced_on

    Ordering:
    - Transactional doctypes are ordered chronologically by
      posting_date ASC, posting_time ASC so stock/valuation-affecting
      documents land in the correct sequence (not the order they happened
      to be fetched/modified).
    - Doctypes without those fields (master data) fall back to
      modified ASC.
    - modified ASC is always appended as a stable tiebreaker.
    """

    meta = frappe.get_meta(external_doctype)
    order_fields = []
    if meta.has_field("posting_date"):
        order_fields.append("posting_date ASC")
    if meta.has_field("posting_time"):
        order_fields.append("posting_time ASC")
    if meta.has_field("creation"):
        order_fields.append("creation ASC")
    order_fields.append("name ASC")
    order_fields.append("modified ASC")

    filter_clauses, values = _external_doc_sync_filters(external_doctype, config)
    where_clauses = [
        "(`tab{0}`.`last_synced_on` IS NULL OR `tab{0}`.`modified` > `tab{0}`.`last_synced_on`)".format(
            external_doctype
        )
    ]
    where_clauses.extend(f"({clause})" for clause in filter_clauses)

    return frappe.db.sql(
        f"""
        SELECT name
        FROM `tab{external_doctype}`
        WHERE {" AND ".join(where_clauses)}
        ORDER BY {", ".join(order_fields)}
        """,
        values,
        pluck=True,
    )


def get_external_doc_names_for_config(external_doctype, config=None):
    filter_clauses, values = _external_doc_sync_filters(external_doctype, config)
    where_clauses = [f"({clause})" for clause in filter_clauses]
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    return frappe.db.sql(
        f"""
        SELECT name
        FROM `tab{external_doctype}`
        {where_sql}
        ORDER BY name ASC
        LIMIT {LINK_AUDIT_MAX_DOCS_PER_DOCTYPE}
        """,
        values,
        pluck=True,
    )
