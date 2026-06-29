# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalLead(Document):
	pass

import frappe
import json
from frappe.utils import cint, flt
from frappe.model.naming import set_new_name


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_leads(names=None):

    if isinstance(names, str):
        names = json.loads(names)

    if not names:
        names = frappe.get_all("External Lead", pluck="name")

    batch_size = 200

    for i in range(0, len(names), batch_size):
        batch = names[i:i + batch_size]

        frappe.enqueue(
            "emkan_insights.emkan_insights.doctype.external_lead.external_lead.sync_lead_batch",
            queue="long",
            timeout=5000,
            names=batch
        )

    return f"{len(names)} Lead(s) queued for sync"


# ==========================================================
# BATCH SYNC
# ==========================================================

def sync_lead_batch(names):

    success = 0
    failed = 0

    for idx, source_name in enumerate(names, start=1):

        try:
            sync_single_lead(source_name)
            success += 1

            if idx % 20 == 0:
                frappe.db.commit()

        except Exception:

            failed += 1

            frappe.log_error(
                title=f"External Lead Sync Failed: {source_name}",
                message=frappe.get_traceback()
            )

    frappe.db.commit()

    return {
        "success": success,
        "failed": failed
    }


# ==========================================================
# HELPERS
# ==========================================================

def get_default_company():
    """Resolve a fallback company that doesn't depend on the
    background job's session user."""
    company = frappe.defaults.get_user_default("Company")
    if not company:
        company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        company = frappe.get_cached_value("Company", {}, "name")
    return company


# ==========================================================
# SINGLE LEAD SYNC
# ==========================================================

def sync_single_lead(source_name):

    # NOTE: verify this is the correct source doctype for your setup.
    # The list view / get_all calls use "External Lead", so that's
    # what's used here. Change back to "External Item" if that is
    # actually the intended source doctype.
    src = frappe.get_doc("External Lead", source_name)
    target_name = getattr(src, "remote_id", None)

    existing = None
    if target_name and frappe.db.exists("Lead", target_name):
        existing = target_name

    # ------------------------------------------------------
    # BUILD LEAD DOC
    # ------------------------------------------------------

    if existing:
        lead = frappe.get_doc("Lead", existing)
    else:
        lead = frappe.new_doc("Lead")
        if target_name:
            lead.name = target_name
            lead.flags.name_set = True
        else:
            # Generate the name up front so child table rows
            # (appended below) get the correct parent value,
            # and so db_insert() has a name to write.
            set_new_name(lead)

    # ------------------------------------------------------
    # MAP FIELDS
    # ------------------------------------------------------

    lead.update({

        "custom_remote_id": src.name,
        "custom_source_site": getattr(src, "source_site", None),

        "lead_name": getattr(src, "full_name", None) or getattr(src, "first_name", None) or src.name,
        "company_name": getattr(src, "organization_name", None),
        "salutation": getattr(src, "salutation", None),
        "first_name": getattr(src, "first_name", None),
        "middle_name": getattr(src, "middle_name", None),
        "last_name": getattr(src, "last_name", None),
        "job_title": getattr(src, "job_title", None),
        "gender": getattr(src, "gender", None),
        "source": getattr(src, "source", None),
        "lead_owner": getattr(src, "lead_owner", None),
        "from_customer": getattr(src, "from_customer", None),
        "lead_type": getattr(src, "lead_type", None),
        "request_type": getattr(src, "request_type", None),
        "email_id": getattr(src, "email", None),
        "website": getattr(src, "website", None),
        "mobile_no": getattr(src, "mobile_no", None),
        "whatsapp_no": getattr(src, "whatsapp", None),
        "phone": getattr(src, "phone", None),
        "phone_ext": getattr(src, "phone_ext", None),
        "no_of_employees": getattr(src, "no_of_employees", None),
        "annual_revenue": flt(getattr(src, "annual_revenue", 0)),
        "industry": getattr(src, "industry", None),
        "market_segment": getattr(src, "market_segment", None),
        "territory": getattr(src, "territory", None),
        "fax": getattr(src, "fax", None),
        "city": getattr(src, "city", None),
        "state": getattr(src, "state_province", None),
        "country": getattr(src, "country", None),
        "qualification_status": getattr(src, "qualification_status", None),
        "qualified_by": getattr(src, "qualified_by", None),
        "qualified_on": getattr(src, "qualified_on", None),
        "campaign_name": getattr(src, "campaign_name", None),
        "company": getattr(src, "company", None) or get_default_company(),
        "language": getattr(src, "print_language", None),
        "image": getattr(src, "image", None),
        "title": getattr(src, "title", None),
        "disabled": cint(getattr(src, "disabled", 0)),
        "unsubscribed": cint(getattr(src, "unsubscribed", 0)),
        "blog_subscriber": cint(getattr(src, "blog_subscriber", 0)),

        "docstatus": cint(getattr(src, "docstatus", 0)),

    })

    status = getattr(src, "status", None)
    if status:
        lead.status = status

    # ------------------------------------------------------
    # SYNC CHILD TABLES
    # ------------------------------------------------------

    def sync_child_table(target_field, source_field, field_map):
        lead.set(target_field, [])
        if not hasattr(src, source_field):
            return
        rows = getattr(src, source_field, [])
        for row in rows:
            new_row = {}
            for target_key, source_key in field_map.items():
                val = getattr(row, source_key, None)
                if val is not None:
                    new_row[target_key] = val
            if new_row:
                lead.append(target_field, new_row)

    sync_child_table("notes", "notes", {
        "added_by": "added_by",
        "added_on": "added_on",
        "note": "note"
    })

    # ------------------------------------------------------
    # SAVE WITHOUT HOOKS (bypasses before_insert contact creation)
    # ------------------------------------------------------

    lead.flags.ignore_permissions = True
    lead.flags.ignore_mandatory = True
    lead.flags.ignore_validate = True
    lead.flags.ignore_links = True

    if existing:
        # Update existing - use db_update to bypass hooks
        lead.db_update()
        # Update all child tables in one call (no fieldname argument)
        lead.update_children()
    else:
        # Insert new - use db_insert to bypass ALL hooks
        lead.db_insert()
        # Insert child table rows manually
        for df in lead.meta.get_table_fields():
            for d in lead.get(df.fieldname):
                d.db_insert()

    return lead.name