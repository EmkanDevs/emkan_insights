# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document


class ExternalModeofPayment(Document):
    pass


@frappe.whitelist()
def sync_external_docs(source_doctype, names):
    """Sync External Mode of Payment to Mode of Payment master."""
    if isinstance(names, str):
        names = json.loads(names)
    
    results = []
    
    for name in names:
        try:
            src = frappe.get_doc(source_doctype, name)
            
            # Skip if already exists
            if frappe.db.exists("Mode of Payment", src.name):
                results.append({
                    "name": name,
                    "status": "skipped",
                    "reason": "Already exists"
                })
                continue
            
            doc = frappe.new_doc("Mode of Payment")
            doc.mode_of_payment = src.name
            doc.enabled = src.get("enabled", 1)
            doc.type = src.get("type", "General")
            
            # Copy other fields
            for field, value in src.as_dict().items():
                if field not in {
                    "name", "owner", "creation", "modified", "modified_by",
                    "docstatus", "idx", "doctype", "__last_sync_on",
                    "parent", "parentfield", "parenttype"
                } and hasattr(doc, field):
                    doc.set(field, value)
            
            # Ignore mandatory validations
            doc.insert(
                ignore_permissions=True,
                ignore_mandatory=True
            )
            
            results.append({
                "name": name,
                "status": "synced",
                "mode_of_payment": doc.name
            })
            
        except Exception:
            error = frappe.get_traceback()
            frappe.log_error(
                title=f"Mode of Payment Sync Error: {name}",
                message=error
            )
            results.append({"name": name, "status": "failed"})
    
    return results