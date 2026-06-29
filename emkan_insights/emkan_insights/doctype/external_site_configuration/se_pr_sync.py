import frappe
import requests
import json
from frappe.utils import now

@frappe.whitelist()
def force_sync_transaction(site_url, api_key, api_secret, doctype, child_docname):
    headers = {
        'Authorization': f'token {api_key}:{api_secret}',
        'Content-Type': 'application/json'
    }
    
    remote_url = f"{site_url.rstrip('/')}/api/resource/{doctype}"
    params = {
        "fields": json.dumps(["*"]),
        "limit_page_length": 20,
        "order_by": "modified desc"
    }

    try:
        response = requests.get(remote_url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        remote_docs = response.json().get('data', [])
    except Exception as e:
        return {"status": "failed", "error": str(e)}

    # LOGGING: This will show up in your browser console or bench logs
    frappe.logger("external_sync").info(f"Force Sync: Found {len(remote_docs)} records for {doctype}")

    count = 0
    # Map to your "External" doctype naming convention
    local_dt = f"External {doctype}"

    for entry in remote_docs:
        doc_name = entry.get("name")
        
        # Check if it exists locally
        if frappe.db.exists(local_dt, doc_name):
            doc = frappe.get_doc(local_dt, doc_name)
        else:
            doc = frappe.new_doc(local_dt)
            doc.name = doc_name

        try:
            # Dynamic Field Mapping
            for field, value in entry.items():
                if hasattr(doc, field) and field not in ["name", "doctype", "owner"]:
                    doc.set(field, value)

            # BYPASS ALL VALIDATION
            doc.flags.ignore_permissions = True
            doc.flags.ignore_links = True
            doc.flags.ignore_validate = True
            doc.flags.ignore_mandatory = True 

            doc.save(ignore_permissions=True)
            count += 1
        except Exception:
            frappe.log_error(f"Force Sync Error on {doc_name}", frappe.get_traceback())
            continue

    frappe.db.commit()
    
    last_sync = now()
    frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
    frappe.db.commit()

    return {
        "status": "success",
        "count": count,
        "fetched": len(remote_docs), # New: returning how many were fetched
        "last_sync": last_sync
    }