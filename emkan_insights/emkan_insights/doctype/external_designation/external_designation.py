# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs

class ExternalDesignation(Document):
    pass


import frappe
import json
from emkan_insights.emkan_insights.external_sync_utils import log_record_failure


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
    # EXISTING: Update via ORM (doc.save) instead of raw SQL
    # ----------------------------------------------------------
    if existing_name:
        doc = frappe.get_doc("Designation", existing_name)
        
        doc.flags.ignore_permissions = True

        doc.designation_name = designation_name
        doc.description = description
        
        if has_remote_id:
            doc.remote_id = src.remote_id
        if has_source_site:
            doc.source_site = source_site

        doc.save()  # ORM save — writes to the transaction, no explicit commit needed
        final_name = doc.name

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

        doc.save()  # ORM save — writes to the transaction, no explicit commit needed
        final_name = doc.name

    # ----------------------------------------------------------
    # Write back the Designation name as remote_id on External Designation
    # ----------------------------------------------------------
    ext_meta = frappe.get_meta("External Designation")
    if ext_meta.has_field("remote_id"):
        # Use db_set on the source doc (ORM equivalent of frappe.db.set_value)
        src.db_set("remote_id", final_name, update_modified=False)

    frappe.clear_document_cache("Designation", final_name)
    return frappe.get_doc("Designation", final_name)


# ==========================================================
# ENTRY POINTS
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_designation_docs(source_doctype: str, names, sync_now=False):
    """
    Sync External Designation records to the Designation doctype.

    When called from the sequential pipeline (sync_now=True), runs synchronously.
    When called from the client, enqueues a background job.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if sync_now:
        return sync_bulk_designations(names)

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

    All database writes (save/db_set) happen within a single transaction.
    Frappe automatically commits the transaction when the background job finishes.
    """
    results = []

    for name in names:
        try:
            
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

            # Frappe owns the transaction boundary for this job.

        except Exception as e:
            log_record_failure("Designation", name, str(e))
            raise

    return results
