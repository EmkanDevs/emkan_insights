# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ExternalLead(Document):
    pass


import json
from frappe.utils import cint, flt
from frappe.model.naming import set_new_name


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

NON_DATA_FIELDS = {
    "naming_series",
    "address_html", "contact_html", "notes_html",
    "open_activities_html", "all_activities_html",
    "col_break123", "column_break2", "column_break_1",
    "column_break_16", "column_break_20", "column_break_22",
    "column_break_28", "column_break_31", "column_break_38",
    "column_break_50", "column_break_64",
    "contact_info_tab", "address_section", "organization_section",
    "qualification_tab", "other_info_tab", "activities_tab",
    "all_activities_section", "notes_tab", "dashboard_tab",
}

# Added custom_remote_id to handled separately
HANDLED_SEPARATELY = {"company", "docstatus", "notes", "lead_name", "title", "custom_remote_id"}


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
    company = frappe.defaults.get_user_default("Company")
    if not company:
        company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        company = frappe.get_cached_value("Company", {}, "name")
    return company


def _get_company_abbr(company):
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(f"Company '{company}' has no abbreviation set")
    return abbr


def _build_target_name(company_abbr, base_id):
    base_id = (base_id or "").strip()
    if not base_id:
        return None

    if base_id.startswith(f"{company_abbr}-"):
        return base_id

    return f"{company_abbr}-{base_id}"


# ==========================================================
# SINGLE LEAD SYNC
# ==========================================================

def sync_single_lead(source_name):

    src = frappe.get_doc("External Lead", source_name)

    company = getattr(src, "company", None) or get_default_company()
    company_abbr = _get_company_abbr(company)

    # Use the External Lead's name as the remote_id
    remote_id = src.name
    
    # Build target name with company prefix
    target_name = _build_target_name(company_abbr, remote_id)

    # Check existing by custom_remote_id OR by target_name
    existing = None
    if frappe.db.exists("Lead", {"custom_remote_id": remote_id}):
        existing = frappe.db.get_value("Lead", {"custom_remote_id": remote_id}, "name")
    elif target_name and frappe.db.exists("Lead", target_name):
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
            set_new_name(lead)

    # ------------------------------------------------------
    # MAP FIELDS
    # ------------------------------------------------------

    lead_meta = frappe.get_meta("Lead")
    valid_columns = set(lead_meta.get_valid_columns())

    src_data = src.as_dict()

    for fieldname, value in src_data.items():
        if fieldname in SYSTEM_FIELDS:
            continue
        if fieldname in NON_DATA_FIELDS:
            continue
        if fieldname in HANDLED_SEPARATELY:
            continue
        if fieldname not in valid_columns:
            continue
        lead.set(fieldname, value)

    lead.annual_revenue = flt(getattr(src, "annual_revenue", 0))
    lead.disabled = cint(getattr(src, "disabled", 0))
    lead.unsubscribed = cint(getattr(src, "unsubscribed", 0))
    lead.blog_subscriber = cint(getattr(src, "blog_subscriber", 0))

    lead.company = company
    lead.docstatus = cint(getattr(src, "docstatus", 0))

    # CRITICAL: Set custom_remote_id to track sync identity
    lead.custom_remote_id = remote_id

    lead.lead_name = (
        getattr(src, "lead_name", None)
        or getattr(src, "first_name", None)
        or src.name
    )

    status = getattr(src, "status", None)
    if status:
        lead.status = status

    # ------------------------------------------------------
    # SYNC CHILD TABLES
    # ------------------------------------------------------

    def sync_child_table(target_field, source_field, child_doctype):
        lead.set(target_field, [])
        if not hasattr(src, source_field):
            return

        child_meta = frappe.get_meta(child_doctype)
        child_valid_columns = set(child_meta.get_valid_columns())

        rows = getattr(src, source_field, []) or []
        for row in rows:
            row_data = row.as_dict()
            new_row = {}
            for fieldname, value in row_data.items():
                if fieldname in SYSTEM_FIELDS:
                    continue
                if fieldname not in child_valid_columns:
                    continue
                if value is None:
                    continue
                new_row[fieldname] = value
            if new_row:
                lead.append(target_field, new_row)

    sync_child_table("notes", "notes", "CRM Note")

    # ------------------------------------------------------
    # SAVE WITHOUT HOOKS
    # ------------------------------------------------------

    lead.flags.ignore_permissions = True
    lead.flags.ignore_mandatory = True
    lead.flags.ignore_validate = True
    lead.flags.ignore_links = True

    if existing:
        lead.db_update()
        lead.update_children()
    else:
        lead.db_insert()
        for df in lead.meta.get_table_fields():
            for d in lead.get(df.fieldname):
                d.db_insert()

    return lead.name