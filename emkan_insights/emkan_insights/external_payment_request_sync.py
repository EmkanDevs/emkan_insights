import frappe
import json
from frappe.utils import flt, getdate

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_FIELDS = {
    "subscription_plans", "remote_id", "custom_remote_id",
    "naming_series", "name", "amended_from", "source_site", "custom_source_site"
}

LINK_DOCTYPES = {
    "Purchase Invoice", "Purchase Order", "Purchase Receipt",
    "Sales Invoice", "Sales Order", "Payment Request"
}


def resolve_local_reference(reference_doctype, reference_name, company_abbr=None):
    """Resolve external reference name to local with company abbreviation prefix."""
    if not reference_name or not reference_doctype:
        return None

    if reference_doctype not in LINK_DOCTYPES:
        return reference_name

    # 1. Check prefixed version first
    if company_abbr and not reference_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{reference_name}"
        if frappe.db.exists(reference_doctype, prefixed):
            return prefixed

    # 2. Check remote_id lookup
    meta = frappe.get_meta(reference_doctype)
    for field in ("remote_id", "custom_remote_id"):
        if meta.has_field(field):
            local_name = frappe.db.get_value(reference_doctype, {field: reference_name}, "name")
            if local_name:
                return local_name

    # 3. Check if raw name exists
    if frappe.db.exists(reference_doctype, reference_name):
        return reference_name

    return None


@frappe.whitelist()
def sync_payment_request_docs(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    names = names or []
    results = []

    for name in names:
        try:
            pr_name = sync_external_payment_request(source_doctype, name)
            results.append({"name": pr_name, "status": "success"})
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Payment Request Sync Failed: {name}")
            results.append({"name": name, "status": "failed"})

    return results


def sync_external_payment_request(source_doctype, external_name):
    external = frappe.get_doc(source_doctype, external_name)

    remote_id = external.name
    company_abbr = frappe.db.get_value("Company", external.company, "abbr") or ""
    target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

    # Check existing by custom_remote_id
    existing_pr = frappe.db.get_value("Payment Request", {"custom_remote_id": remote_id}, "name")
    if existing_pr:
        return existing_pr

    if not getattr(external, "reference_doctype", None) or not getattr(external, "reference_name", None):
        frappe.throw("reference_doctype and reference_name are required")

    pr = frappe.new_doc("Payment Request")

    # Set name
    pr.name = target_name
    pr.flags.name_set = True

    # Set remote_id on correct field
    pr.custom_remote_id = remote_id

    # Set source_site on correct field
    if hasattr(external, "source_site"):
        pr.custom_source_site = external.source_site

    # Flags
    pr.flags.ignore_permissions = True
    pr.flags.ignore_mandatory = True
    pr.flags.ignore_links = True
    pr.flags.ignore_validate = True
    pr.flags.ignore_naming_series = True

    # Naming series
    pr.naming_series = getattr(external, "naming_series", None) or "ACC-PRQ-.YYYY.-"

    # Copy fields
    for field, value in external.as_dict().items():
        if field in SYSTEM_FIELDS or field in IGNORE_FIELDS:
            continue
        if hasattr(pr, field):
            pr.set(field, value)

    # Resolve reference_name with company abbreviation
    ref_doctype = getattr(external, "reference_doctype", None)
    ref_name = getattr(external, "reference_name", None)
    
    if ref_doctype and ref_name:
        local_ref = resolve_local_reference(ref_doctype, ref_name, company_abbr)
        pr.reference_doctype = ref_doctype
        pr.reference_name = local_ref or ref_name

    # Set amounts
    pr.grand_total = flt(getattr(external, "grand_total", 0))
    pr.outstanding_amount = flt(getattr(external, "outstanding_amount", pr.grand_total))

    # Date
    if getattr(external, "transaction_date", None):
        pr.transaction_date = getdate(external.transaction_date)

    # Subscription plans child table
    pr.set("subscription_plans", [])
    for row in (external.get("subscription_plans") or []):
        pr.append("subscription_plans", {
            "plan": getattr(row, "plan", None),
            "qty": getattr(row, "qty", 1),
            "cost": getattr(row, "cost", 0),
        })

    # Insert
    pr.insert(
        ignore_permissions=True,
        ignore_links=True,
        ignore_mandatory=True
    )

    # Update external with local name
    external.db_set("remote_id", pr.name)
    frappe.db.commit()

    return pr.name