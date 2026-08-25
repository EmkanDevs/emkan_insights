import frappe
import json
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.sync_guard import set_source_site_fields

@frappe.whitelist()
@filter_kwargs
def sync_tax_category_docs(source_doctype=None, names=None):

    if names:
        if isinstance(names, str):
            names = json.loads(names)

        return sync_bulk_tax_categories(names)

    names = frappe.get_all(
        "External Tax Category",
        pluck="name",
        limit_page_length=0
    )

    if not names:
        return "No External Tax Category records found"

    frappe.enqueue(
        "emkan_insights.emkan_insights.external_tax_category_sync.sync_bulk_tax_categories",
        queue="long",
        timeout=3000,
        names=names
    )

    return f"{len(names)} Tax Categories queued for sync"


def sync_bulk_tax_categories(names):

    success = 0
    failed = 0

    existing = {
        d.remote_id: d.name
        for d in frappe.get_all(
            "Tax Category",
            fields=["name", "remote_id"],
            filters={"remote_id": ["is", "set"]}
        )
    }

    docs = frappe.get_all(
        "External Tax Category",
        filters={"name": ["in", names]},
        fields=["name"],
        limit_page_length=0
    )

    for row in docs:
        try:
            src = frappe.get_doc("External Tax Category", row.name)
            sync_started_at = src.modified

            upsert_tax_category(
                src,
                existing_name=existing.get(src.remote_id)
            )

            src.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

            success += 1

        except Exception:
            failed += 1
            frappe.log_error(
                frappe.get_traceback(),
                f"Tax Category Sync Failed - {row.name}"
            )

    return {
        "success": success,
        "failed": failed
    }


def upsert_tax_category(src, existing_name=None):

    if not src.remote_id:
        raise Exception(f"Remote ID missing for {src.name}")

    if existing_name:
        doc = frappe.get_doc("Tax Category", existing_name)
    else:
        doc = frappe.new_doc("Tax Category")

    doc.title = src.title
    doc.disabled = src.disabled or 0

    if hasattr(doc, "remote_id"):
        doc.remote_id = src.remote_id

    set_source_site_fields(doc, site_url=getattr(src, "source_site", None))

    doc.flags.ignore_permissions = True

    if existing_name:
        doc.save()
    else:
        doc.insert()

    return doc
