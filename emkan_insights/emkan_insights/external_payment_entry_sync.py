import frappe
import json


# ==========================================================
# CONSTANTS
# ==========================================================

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

EXPLICITLY_HANDLED_HEADER_FIELDS = {
    "references", "taxes", "deductions", "remote_id", "custom_remote_id"
}

LOCAL_PARENTTYPE = "Payment Entry"


# ==========================================================
# HELPERS
# ==========================================================

def _get_company_abbr(company):
    if not company:
        return ""
    return frappe.db.get_value("Company", company, "abbr") or ""


def _build_target_name(company_abbr, remote_id):
    """Canonical local PE name: {company_abbr}-{remote_id} (no double-prefix)."""
    remote_id = (remote_id or "").strip()
    if not remote_id:
        return remote_id
    if company_abbr and remote_id.startswith(f"{company_abbr}-"):
        return remote_id
    return f"{company_abbr}-{remote_id}" if company_abbr else remote_id


def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None, company=None):
    """
    Resolve a linked local document for PE references.

    1. Prefixed local name: {company_abbr}-{remote_name}
    2. remote_id / custom_remote_id lookup (company-scoped when possible)
    3. Raw name existence check
    """
    if not remote_or_local_name or not doctype:
        return None

    if not frappe.db.exists("DocType", doctype):
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    meta = frappe.get_meta(doctype)
    for remote_field in ("remote_id", "custom_remote_id"):
        if not meta.has_field(remote_field):
            continue
        filters = {remote_field: remote_or_local_name}
        if company and meta.has_field("company"):
            filters["company"] = company
        local_name = frappe.db.get_value(doctype, filters, "name")
        if local_name:
            return local_name
        # fallback without company if scoped lookup misses
        if company and meta.has_field("company"):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def _find_existing_payment_entry(remote_id, target_name, company):
    """
    Find an already-synced local Payment Entry for this remote id.

    Scoped by company so CPC ACC-PAY-2026-00001 does not collide with an
    SEC / IMC Payment Entry that happens to share the same remote name.
    """
    if remote_id:
        filters = {"remote_id": remote_id}
        if company:
            filters["company"] = company
        existing = frappe.db.get_value("Payment Entry", filters, "name")
        if existing:
            return existing

    if target_name and frappe.db.exists("Payment Entry", target_name):
        if not company or frappe.db.get_value("Payment Entry", target_name, "company") == company:
            return target_name

    return None


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_payment_entry_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            result = sync_external_payment_entry(source_doctype, name)
            results.append({"name": name, "status": "synced", "payment_entry": result})

        except Exception:
            error = frappe.get_traceback()

            frappe.log_error(
                title=f"Payment Entry Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed"
            })

    return results


# ==========================================================
# MAIN SYNC FUNCTION
# ==========================================================

def sync_external_payment_entry(source_doctype, external_name):

    src = frappe.get_doc(source_doctype, external_name)
    remote_id = src.name
    company = src.company
    company_abbr = _get_company_abbr(company)
    target_name = _build_target_name(company_abbr, remote_id)

    # ------------------------------------------------------
    # Prevent duplicate (company-scoped)
    # ------------------------------------------------------
    existing = _find_existing_payment_entry(remote_id, target_name, company)
    if existing:
        return existing

    # ------------------------------------------------------
    # Dependency Checks
    # ------------------------------------------------------
    if src.party_type and src.party:
        if not frappe.db.exists(src.party_type, src.party):
            frappe.throw(f"{src.party_type} {src.party} not found. Sync master first.")

    if src.paid_from and not frappe.db.exists("Account", src.paid_from):
        frappe.throw(f"Account {src.paid_from} not found.")

    if src.paid_to and not frappe.db.exists("Account", src.paid_to):
        frappe.throw(f"Account {src.paid_to} not found.")

    # ------------------------------------------------------
    # Create Payment Entry with company-prefixed name
    # ------------------------------------------------------
    doc = frappe.new_doc("Payment Entry")
    doc.remote_id = remote_id
    doc.name = target_name
    doc.flags.name_set = True

    # ------------------------------------------------------
    # Copy Main Fields
    # ------------------------------------------------------
    for field, value in src.as_dict().items():
        if (
            field not in SYSTEM_FIELDS
            and field not in EXPLICITLY_HANDLED_HEADER_FIELDS
            and hasattr(doc, field)
        ):
            doc.set(field, value)

    # Ensure company + remote_id stick after generic copy
    doc.company = company
    doc.remote_id = remote_id

    # ------------------------------------------------------
    # References Table — resolve reference_name to local docs
    # ------------------------------------------------------
    doc.set("references", [])

    for row in src.get("references") or []:
        ref_row = {}

        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS:
                ref_row[field] = value

        ref_doctype = row.get("reference_doctype")
        ref_name = row.get("reference_name")
        if ref_doctype and ref_name:
            local_ref = resolve_local_doc(
                ref_doctype, ref_name, company_abbr=company_abbr, company=company
            )
            if local_ref:
                ref_row["reference_name"] = local_ref
            else:
                frappe.log_error(
                    title=f"PE Sync - unresolved reference {ref_doctype}/{ref_name}",
                    message=(
                        f"External Payment Entry {remote_id}: could not resolve "
                        f"{ref_doctype} '{ref_name}' to a local document "
                        f"(tried {company_abbr}-{ref_name} and remote_id). "
                        f"Leaving remote reference value as-is."
                    ),
                )

        doc.append("references", ref_row)

    # ------------------------------------------------------
    # Taxes / Deductions (copy when present)
    # ------------------------------------------------------
    for table_field in ("taxes", "deductions"):
        if not hasattr(src, table_field) or not hasattr(doc, table_field):
            continue
        doc.set(table_field, [])
        for row in src.get(table_field) or []:
            child = {}
            for field, value in row.as_dict().items():
                if field not in SYSTEM_FIELDS:
                    child[field] = value
            doc.append(table_field, child)

    # ------------------------------------------------------
    # Safe Flags
    # ------------------------------------------------------
    doc.flags.ignore_validate = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_links = True
    doc.flags.ignore_naming_series = True

    # ------------------------------------------------------
    # Insert
    # ------------------------------------------------------
    doc.insert(
        ignore_permissions=True,
        ignore_links=True,
        ignore_mandatory=True
    )

    # ------------------------------------------------------
    # Naming safety net → company-prefixed target_name
    # ------------------------------------------------------
    if doc.name != target_name:
        if not frappe.db.exists("Payment Entry", target_name):
            old_name = doc.name

            frappe.db.sql("""
                UPDATE `tabPayment Entry`
                SET name = %s
                WHERE name = %s
            """, (target_name, old_name))

            frappe.db.sql("""
                UPDATE `tabPayment Entry Reference`
                SET parent = %s
                WHERE parent = %s AND parenttype = %s
            """, (target_name, old_name, LOCAL_PARENTTYPE))

            for child_dt in ("Advance Taxes and Charges", "Payment Entry Deduction"):
                if frappe.db.exists("DocType", child_dt):
                    frappe.db.sql(f"""
                        UPDATE `tab{child_dt}`
                        SET parent = %s
                        WHERE parent = %s AND parenttype = %s
                    """, (target_name, old_name, LOCAL_PARENTTYPE))

            frappe.db.commit()
            doc.name = target_name
        else:
            frappe.log_error(
                f"Payment Entry Sync: could not rename {doc.name} to "
                f"{target_name} because that name already exists.",
                "ExternalPaymentEntry Sync - name collision"
            )

    # Keep remote_id = external name (unprefixed source identity)
    frappe.db.set_value("Payment Entry", doc.name, "remote_id", remote_id, update_modified=False)

    # ------------------------------------------------------
    # Sync Docstatus
    # ------------------------------------------------------
    if src.docstatus == 1:
        doc.submit()

    elif src.docstatus == 2:
        doc.submit()
        doc.cancel()

    return doc.name
