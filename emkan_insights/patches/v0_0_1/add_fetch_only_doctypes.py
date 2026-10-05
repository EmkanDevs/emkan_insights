import frappe

from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
    FETCH_ONLY_DOCTYPE_MAPPINGS,
)


def execute():
    """Add fetch-only mappings to the template and existing site configs."""
    if not frappe.db.exists("DocType", "Site Configuration Settings"):
        return

    settings = frappe.get_single("Site Configuration Settings")
    if _append_missing_rows(settings):
        settings.save(ignore_permissions=True)

    for name in frappe.get_all("External Site Configuration", pluck="name"):
        config = frappe.get_doc("External Site Configuration", name)
        if _append_missing_rows(config):
            config.save(ignore_permissions=True)


def _append_missing_rows(doc):
    existing = {row.ref_doctype for row in doc.fetch_data or []}
    changed = False

    for ref_doctype, exported_doctype in FETCH_ONLY_DOCTYPE_MAPPINGS.items():
        if ref_doctype in existing:
            continue

        doc.append(
            "fetch_data",
            {
                "ref_doctype": ref_doctype,
                "exported_doctype": exported_doctype,
                "abbr_required": 1,
            },
        )
        existing.add(ref_doctype)
        changed = True

    return changed
