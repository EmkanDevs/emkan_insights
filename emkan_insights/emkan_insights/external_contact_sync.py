import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on"
}

@frappe.whitelist()
def sync_external_contact_docs(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    results = []
    for name in names:
        try:
            ext_contact = frappe.get_doc(source_doctype, name)

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
                    "email_ids", "phone_nos", "links", "naming_series"
                ] and hasattr(contact, field):
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
                    if row.link_doctype == "Lead":
                        continue
                    if not frappe.db.exists(row.link_doctype, row.link_name):
                        continue

                    contact.append("links", {
                        "link_doctype": row.link_doctype,
                        "link_name": row.link_name,
                        "link_title": row.link_title or ""
                    })

            # --- INSERT WITH FLAGS ---
            contact.insert(
                ignore_permissions=True, 
                ignore_links=True,
                ignore_mandatory=True
            )
            
            results.append({"name": contact.name, "status": "synced"})

        except Exception as e:
            error_msg = f"Contact Sync Error for {name}: {str(e)}\n{frappe.get_traceback()}"
            frappe.log_error("Contact Sync Error", error_msg)
            # Return error in response so you can debug
            results.append({
                "name": name, 
                "status": "error", 
                "error": str(e)
            })

    return results