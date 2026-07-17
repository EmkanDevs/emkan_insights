# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalDesignation(Document):
	pass


import frappe
import json


# ==========================================================
# HELPERS
# ==========================================================

def _find_existing_designation(remote_id, designation_name):
    """
    Look up an existing Designation by remote_id first, then by exact name.
    Designations use designation_name as the document name.
    """
    if remote_id:
        existing = frappe.db.get_value("Designation", {"remote_id": remote_id}, "name")
        if existing:
            return existing

    if designation_name and frappe.db.exists("Designation", designation_name):
        return designation_name

    return None


# ==========================================================
# MAIN UPSERT
# ==========================================================

def upsert_designation(src, existing_name):
    """
    Upsert a Designation record from an External Designation source.

    - Target name = designation_name (no company-abbr prefix)
    - remote_id written back to External Designation is the Designation name
    """
    designation_name = getattr(src, "designation_name", None) or src.name
    description = getattr(src, "description", None)
    source_site = getattr(src, "source_site", None)

    # Check which optional custom fields exist on the target Designation doctype
    desig_meta = frappe.get_meta("Designation")
    has_remote_id = desig_meta.has_field("remote_id")
    has_source_site = desig_meta.has_field("source_site")

    # ----------------------------------------------------------
    # EXISTING: raw SQL update to bypass ORM
    # ----------------------------------------------------------
    if existing_name:
        pairs = [
            ("designation_name", designation_name),
            ("description", description),
        ]
        if has_remote_id:
            pairs.append(("remote_id", src.remote_id))
        if has_source_site:
            pairs.append(("source_site", source_site))

        set_clause = ", ".join(f"`{col}` = %s" for col, _ in pairs) + ", modified = NOW()"
        values = [v for _, v in pairs] + [existing_name]

        frappe.db.sql(
            f"UPDATE `tabDesignation` SET {set_clause} WHERE name = %s",
            tuple(values),
        )
        frappe.db.commit()
        final_name = existing_name

    else:
        # ----------------------------------------------------------
        # NEW: ORM insert with all flags disabled
        # ----------------------------------------------------------
        doc = frappe.new_doc("Designation")
        doc.name = designation_name
        doc.flags.name_set = True

        update_dict = {
            "designation_name": designation_name,
            "description": description,
        }
        if has_remote_id:
            update_dict["remote_id"] = src.remote_id
        if has_source_site:
            update_dict["source_site"] = source_site
        doc.update(update_dict)

        doc.flags.ignore_permissions = True
        doc.flags.ignore_validate = True
        doc.flags.ignore_links = True
        doc.flags.ignore_mandatory = True

        doc.save()
        frappe.db.commit()
        final_name = doc.name

    # ----------------------------------------------------------
    # Write back the Designation name as remote_id on External Designation
    # ----------------------------------------------------------
    ext_meta = frappe.get_meta("External Designation")
    if ext_meta.has_field("remote_id"):
        frappe.db.set_value(
            "External Designation",
            src.name,
            "remote_id",
            final_name,
            update_modified=False,
        )
        frappe.db.commit()

    frappe.clear_document_cache("Designation", final_name)
    return frappe.get_doc("Designation", final_name)


# ==========================================================
# ENTRY POINTS
# ==========================================================

@frappe.whitelist()
def sync_designation_docs(source_doctype: str, names):
    """
    Whitelisted entry point to queue designation sync.
    Called from the list-view Sync button with selected External Designation names.
    """
    if isinstance(names, str):
        names = json.loads(names)

    frappe.enqueue(
        "emkan_insights.emkan_insights.doctype.external_designation.external_designation.sync_bulk_designations",
        queue="long",
        names=names,
        timeout=2000,
    )
    return "Sync Queued"


def sync_bulk_designations(names):
    """
    Bulk sync designations from External Designation to Designation.
    Processed in background queue.
    """
    results = []

    for name in names:
        try:
            frappe.db.commit()
            src = frappe.get_doc("External Designation", name)

            designation_name = getattr(src, "designation_name", None) or src.name
            existing = _find_existing_designation(
                getattr(src, "remote_id", None),
                designation_name,
            )

            doc = upsert_designation(src, existing)

            results.append(
                {
                    "name": name,
                    "status": "success",
                    "designation_name": doc.name,
                }
            )

            if len(results) % 10 == 0:
                frappe.db.commit()

        except Exception as e:
            import traceback

            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }
            )

    frappe.db.commit()
    return results
