# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ExternalOpportunity(Document):
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
    "column_break0", "column_break1", "column_break3",
    "column_break_10", "column_break_17", "column_break_22",
    "column_break_23", "column_break_31", "column_break_33",
    "column_break_36", "column_break_54", "column_break_56",
    "contact_info", "organization_details_section",
    "address_contact_section", "primary_contact_section",
    "section_break_14", "section_break_32", "more_info",
    "lost_detail_section", "items_section", "all_activities_section",
    "activities_tab", "notes_tab", "dashboard_tab"
}

HANDLED_SEPARATELY = {
    "company", "docstatus", "notes", "items", "title",
    "party_name", "opportunity_from",
    "contact_person", "customer_address"
}


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_opportunities(names=None):

    if isinstance(names, str):
        names = json.loads(names)

    if not names:
        names = frappe.get_all("External Opportunity", pluck="name")

    batch_size = 200

    for i in range(0, len(names), batch_size):
        batch = names[i:i + batch_size]

        frappe.enqueue(
            "emkan_insights.emkan_insights.doctype.external_opportunity.external_opportunity.sync_opportunity_batch",
            queue="long",
            timeout=5000,
            names=batch
        )

    return f"{len(names)} Opportunity(ies) queued for sync"


# ==========================================================
# BATCH SYNC
# ==========================================================

def sync_opportunity_batch(names):

    success = 0
    failed = 0

    for idx, source_name in enumerate(names, start=1):

        try:
            sync_single_opportunity(source_name)
            success += 1

            if idx % 20 == 0:
                frappe.db.commit()

        except Exception:

            failed += 1

            frappe.log_error(
                title=f"External Opportunity Sync Failed: {source_name}",
                message=frappe.get_traceback()
            )

    frappe.db.commit()

    return {
        "success": success,
        "failed": failed
    }


# ==========================================================
# HELPERS: COMPANY / NAMING
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
# HELPERS: CROSS-DOCTYPE LINK RESOLUTION
# ==========================================================

def resolve_local_link(doctype, remote_value, company_abbr):
    if not remote_value:
        return None

    if frappe.get_meta(doctype).has_field("remote_id"):
        local_name = frappe.db.get_value(
            doctype, {"remote_id": remote_value}, "name"
        )
        if local_name:
            return local_name

    if frappe.db.exists(doctype, remote_value):
        return remote_value

    if company_abbr and not remote_value.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_value}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    return None


def resolve_local_party(opportunity_from, remote_party_name, company_abbr):
    if not remote_party_name or not opportunity_from:
        return None

    if not frappe.db.exists("DocType", opportunity_from):
        return None

    return resolve_local_link(opportunity_from, remote_party_name, company_abbr)


# ==========================================================
# SINGLE OPPORTUNITY SYNC
# ==========================================================

def sync_single_opportunity(source_name):

    src = frappe.get_doc("External Opportunity", source_name)

    company = getattr(src, "company", None) or get_default_company()
    company_abbr = _get_company_abbr(company)

    remote_id = src.name
    target_name = _build_target_name(company_abbr, remote_id)

    existing = None
    if frappe.db.exists("Opportunity", {"remote_id": remote_id}):
        existing = frappe.db.get_value("Opportunity", {"remote_id": remote_id}, "name")
    elif target_name and frappe.db.exists("Opportunity", target_name):
        existing = target_name

    # ------------------------------------------------------
    # BUILD OPPORTUNITY DOC
    # ------------------------------------------------------

    if existing:
        opportunity = frappe.get_doc("Opportunity", existing)
    else:
        opportunity = frappe.new_doc("Opportunity")
        if target_name:
            opportunity.name = target_name
            opportunity.flags.name_set = True
        else:
            set_new_name(opportunity)

    # ------------------------------------------------------
    # MAP FIELDS (generic copy)
    # ------------------------------------------------------

    opp_meta = frappe.get_meta("Opportunity")
    valid_columns = set(opp_meta.get_valid_columns())

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
        opportunity.set(fieldname, value)

    opportunity.annual_revenue = flt(getattr(src, "annual_revenue", 0))
    opportunity.opportunity_amount = flt(getattr(src, "opportunity_amount", 0))
    opportunity.base_opportunity_amount = flt(getattr(src, "base_opportunity_amount", 0))
    opportunity.probability = flt(getattr(src, "probability", 0))
    opportunity.conversion_rate = flt(getattr(src, "conversion_rate", 0))
    opportunity.total = flt(getattr(src, "total", 0))
    opportunity.base_total = flt(getattr(src, "base_total", 0))

    opportunity.company = company
    opportunity.docstatus = cint(getattr(src, "docstatus", 0))

    opportunity.remote_id = remote_id
    opportunity.source_site = getattr(src, "source_site", None)

    opportunity.title = (
        getattr(src, "title", None)
        or getattr(src, "customer_name", None)
        or src.name
    )

    status = getattr(src, "status", None)
    if status:
        opportunity.status = status

    # ------------------------------------------------------
    # RESOLVE PARTY_NAME (Dynamic Link)
    # ------------------------------------------------------

    opportunity_from = getattr(src, "opportunity_from", None)
    remote_party_name = getattr(src, "party_name", None)

    opportunity.opportunity_from = opportunity_from

    resolved_party = resolve_local_party(
        opportunity_from, remote_party_name, company_abbr
    )

    if resolved_party:
        opportunity.party_name = resolved_party
    elif remote_party_name:
        if not remote_party_name.startswith(f"{company_abbr}-"):
            opportunity.party_name = f"{company_abbr}-{remote_party_name}"
        else:
            opportunity.party_name = remote_party_name

    # ------------------------------------------------------
    # RESOLVE OTHER LINKS
    # ------------------------------------------------------

    remote_contact_person = getattr(src, "contact_person", None)
    if remote_contact_person:
        resolved_contact = resolve_local_link(
            "Contact", remote_contact_person, company_abbr
        )
        opportunity.contact_person = resolved_contact
        if not resolved_contact:
            frappe.log_error(
                title=f"Opportunity Sync - contact not resolved: {source_name}",
                message=f"remote contact_person={remote_contact_person}, company_abbr={company_abbr}. Leaving unset."
            )

    remote_customer_address = getattr(src, "customer_address", None)
    if remote_customer_address:
        resolved_address = resolve_local_link(
            "Address", remote_customer_address, company_abbr
        )
        opportunity.customer_address = resolved_address
        if not resolved_address:
            frappe.log_error(
                title=f"Opportunity Sync - address not resolved: {source_name}",
                message=f"remote customer_address={remote_customer_address}, company_abbr={company_abbr}. Leaving unset."
            )

    # ------------------------------------------------------
    # SYNC CHILD TABLES
    # ------------------------------------------------------

    def sync_child_table(target_field, source_field, child_doctype):
        opportunity.set(target_field, [])
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
            
            # CRITICAL FIX: Map remote_id from source row to custom_remote_id on target
            # External Opportunity Item has 'remote_id', Opportunity Item has 'custom_remote_id'
            if "remote_id" in row_data and "custom_remote_id" in child_valid_columns:
                new_row["custom_remote_id"] = row_data["remote_id"]
            
            if new_row:
                opportunity.append(target_field, new_row)

    sync_child_table("items", "items", "Opportunity Item")
    sync_child_table("notes", "notes", "CRM Note")

    # ------------------------------------------------------
    # SAVE WITHOUT HOOKS
    # ------------------------------------------------------

    opportunity.flags.ignore_permissions = True
    opportunity.flags.ignore_mandatory = True
    opportunity.flags.ignore_validate = True
    opportunity.flags.ignore_links = True

    if existing:
        opportunity.db_update()
        opportunity.update_children()
    else:
        opportunity.db_insert()
        for df in opportunity.meta.get_table_fields():
            for d in opportunity.get(df.fieldname):
                d.db_insert()

    return opportunity.name