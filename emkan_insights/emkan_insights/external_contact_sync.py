import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on"
}

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300

from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.doctype.external_address.external_address import (
    _resolve_dynamic_link,
)


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_contact_docs(source_doctype, names):
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

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"contact_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_contact_sync._sync_external_contact_docs_job",
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

def _sync_external_contact_docs_job(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []
    for name in names:
        try:
            ext_contact = frappe.get_doc(source_doctype, name)
            sync_started_at = ext_contact.modified

            # Check if this remote ID already exists locally
            existing_contact = frappe.db.get_value("Contact", {"remote_id": ext_contact.name}, "name")
            if existing_contact:
                results.append({"name": existing_contact, "status": "exists"})
                continue

            contact = frappe.new_doc("Contact")

            # --- CRITICAL FIXES FOR NAMING ---
            # 1. Clear naming series to prevent autoname conflict
            contact.naming_series = ""
            
            # 2. Force the name field directly
            contact.name = ext_contact.name
            
            # 3. This flag tells Frappe "Use my name, do not run autoname()"
            contact.set("__newname", ext_contact.name)
            
            # 4. Store the remote ID in your custom field for future lookups
            contact.remote_id = ext_contact.name

            # --- Map Names ---
            contact.first_name = ext_contact.first_name or ""
            contact.last_name = ext_contact.last_name or ""
            contact.full_name = ext_contact.full_name or f"{contact.first_name} {contact.last_name}".strip()

            # --- Copy other fields ---
            for field, value in ext_contact.as_dict().items():
                if field not in SYSTEM_FIELDS and field not in [
                    "first_name", "last_name", "remote_id", "name", 
                    "email_ids", "phone_nos", "links", "naming_series" , "source_site" , "user"
                ] and hasattr(contact, field):
                    df = contact.meta.get_field(field)
                    contact.set(field, value)

            # --- Emails ---
            contact.set("email_ids", [])
            primary_email = None
            
            if ext_contact.get("email_ids"):
                for row in ext_contact.email_ids:
                    if not row.email_id:
                        continue
                    is_p = 1 if (row.is_primary and not primary_email) else 0
                    contact.append("email_ids", {
                        "email_id": row.email_id, 
                        "is_primary": is_p
                    })
                    if is_p: 
                        primary_email = row.email_id
                
                if contact.email_ids and not primary_email:
                    contact.email_ids[0].is_primary = 1
                    primary_email = contact.email_ids[0].email_id
            
            contact.email_id = primary_email

            # --- Phones ---
            contact.set("phone_nos", [])
            has_p_phone = False
            has_p_mobile = False
            
            if ext_contact.get("phone_nos"):
                for row in ext_contact.phone_nos:
                    if not row.phone:
                        continue
                    is_p_phone = 1 if (row.is_primary_phone and not has_p_phone) else 0
                    is_p_mobile = 1 if (row.is_primary_mobile_no and not has_p_mobile) else 0
                    if is_p_phone: 
                        has_p_phone = True
                    if is_p_mobile: 
                        has_p_mobile = True
                    contact.append("phone_nos", {
                        "phone": row.phone, 
                        "is_primary_phone": is_p_phone, 
                        "is_primary_mobile_no": is_p_mobile
                    })

            # --- Links ---
            contact.set("links", [])

            if ext_contact.get("links"):
                for row in ext_contact.links:
                    if not row.link_doctype or not row.link_name:
                        continue
                    resolved_link = _resolve_dynamic_link(row.link_doctype, row.link_name)

                    contact.append("links", {
                        "link_doctype": row.link_doctype,
                        "link_name": resolved_link or row.link_name,
                        "link_title": row.link_title or ""
                    })

            # --- INSERT WITH FLAGS ---
            contact.flags.ignore_links = True
            contact.insert(
                ignore_permissions=True, 
            )
            
            results.append({"name": contact.name, "status": "synced"})

            ext_contact.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

        except Exception as e:
            error_msg = f"Contact Sync Error for {name}: {str(e)}\n{frappe.get_traceback()}"
            frappe.log_error("Contact Sync Error", error_msg)
            # Return error in response so you can debug
            results.append({
                "name": name, 
                "status": "error", 
                "error": str(e)
            })
            break

    return results
