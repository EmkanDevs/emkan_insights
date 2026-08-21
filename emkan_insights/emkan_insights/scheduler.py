import frappe

from emkan_insights.emkan_insights.registry.fetch_registry import (
    SPECIAL_FETCHERS,
    DEFAULT_FETCHER,
)

from emkan_insights.emkan_insights.registry.sync_registry import (
    SYNC_METHODS,
)


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
        frappe.log_error(f"Configs: {configs}", "DEBUG")

        for config_name in configs:
            
            config = frappe.get_doc(
                "External Site Configuration",
                config_name,
            )

            frappe.log_error(f"Processing {config.name}", "DEBUG")

            frappe.log_error(
                f"Starting fetch for {config.name}"
            )

            for row in config.fetch_data:
                # if row.ref_doctype != "Lead":
                #     continue

                frappe.log_error(
                    f"Fetching {row.ref_doctype}"
                )

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

                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Failed to fetch {row.ref_doctype} from {config.name}",
                    )

            frappe.log_error(
                f"Completed fetch for {config.name}"
            )

        frappe.log_error(
            "All fetches completed. Fetch phase enqueued — sync will run in the next scheduled job."
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "fetch_scheduler failed",
        )


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

        config = frappe.get_doc(
            "External Site Configuration",
            config_name,
        )

        for row in config.fetch_data:

            if row.ref_doctype != "Material Request":
                continue

            sync_method = SYNC_METHODS.get(row.ref_doctype)

            if not sync_method:
                frappe.log_error(
                    f"No sync method configured for '{row.ref_doctype}'"
                )
                continue

            if row.ref_doctype == "UOM":
                external_doctype = "External Uom"
            else:
                external_doctype = f"External {row.ref_doctype}"
            # external_doctype = f"External {row.ref_doctype}"

            names = get_external_doc_names_to_sync(
                external_doctype,
            )

            frappe.log_error(
                title="Sync Debug",
                message=f"Pending records: {len(names)}"
            )

            if not names:
                frappe.log_error(
                    f"No {row.ref_doctype} records pending sync."
                )
                continue

            frappe.log_error(
                f"Syncing {len(names)} {row.ref_doctype} record(s)."
            )

            try:
                # sync_method(names)
                sync_method(
                    names=names,
                    source_doctype=external_doctype,
                    company=config.company,
                )
                # Commit after each doctype's sync so writes are immediately
                # visible and don't hold an open transaction lock.
                # frappe.db.commit()

            except Exception:
                # frappe.db.rollback()
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Failed to sync {row.ref_doctype}",
                )


def get_external_doc_names_to_sync(external_doctype):
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
    order_fields.append("modified ASC")

    return frappe.db.sql(
        f"""
        SELECT name
        FROM `tab{external_doctype}`
        WHERE
            last_synced_on IS NULL
            OR modified > last_synced_on
        ORDER BY {", ".join(order_fields)}
        """,
        pluck=True,
    )