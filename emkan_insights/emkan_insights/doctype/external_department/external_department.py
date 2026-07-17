# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalDepartment(Document):
	pass


import frappe
import json


# ==========================================================
# HELPERS
# ==========================================================

def _resolve_parent_department(parent_name):
    """
    Resolve parent_department from External Department source.
    Tries direct match, then match by remote_id on the local Department.
    """
    if not parent_name:
        return None

    # Direct match (the local Department might already exist with this name)
    if frappe.db.exists("Department", parent_name):
        return parent_name

    # Try to find via remote_id stored on External Department
    found = frappe.db.get_value(
        "External Department", {"remote_id": parent_name}, "department_name"
    )
    if found and frappe.db.exists("Department", found):
        return found

    # Try partial / LIKE match as a last resort
    found = frappe.db.sql(
        "SELECT name FROM `tabDepartment` WHERE name LIKE %s LIMIT 1",
        (f"%{parent_name}%",),
        as_dict=False,
    )
    if found:
        return found[0][0]

    return None


def _find_existing_department(remote_id, department_name):
    """
    Look up an existing Department by remote_id first, then by exact name.
    Departments use their human-readable department_name as the document name.
    """
    if remote_id:
        # remote_id on Department doc (custom field)
        existing = frappe.db.get_value("Department", {"remote_id": remote_id}, "name")
        if existing:
            return existing

    if department_name and frappe.db.exists("Department", department_name):
        return department_name

    return None


# ==========================================================
# MAIN UPSERT
# ==========================================================

def upsert_department(src, existing_name):
    """
    Upsert a Department record from an External Department source.

    Key difference from Employee sync:
      - Target name = department_name  (NOT company-abbr-prefixed)
      - remote_id written back to External Department is the Department name
    """
    department_name = getattr(src, "department_name", None) or src.name
    parent_department = _resolve_parent_department(
        getattr(src, "parent_department", None)
    )
    company = getattr(src, "company", None)
    is_group = int(getattr(src, "is_group", 0) or 0)
    disabled = int(getattr(src, "disabled", 0) or 0)
    source_site = getattr(src, "source_site", None)

    # Check which optional custom fields exist on the target Department doctype
    dept_meta = frappe.get_meta("Department")
    has_remote_id = dept_meta.has_field("remote_id")
    has_source_site = dept_meta.has_field("source_site")

    # ----------------------------------------------------------
    # EXISTING: raw SQL update to bypass ORM
    # ----------------------------------------------------------
    if existing_name:
        # Build SET clause dynamically based on available columns
        pairs = [
            ("department_name", department_name),
            ("parent_department", parent_department),
            ("company", company),
            ("is_group", is_group),
            ("disabled", disabled),
        ]
        if has_remote_id:
            pairs.append(("remote_id", src.remote_id))
        if has_source_site:
            pairs.append(("source_site", source_site))

        set_clause = ", ".join(f"`{col}` = %s" for col, _ in pairs) + ", modified = NOW()"
        values = [v for _, v in pairs] + [existing_name]

        frappe.db.sql(
            f"UPDATE `tabDepartment` SET {set_clause} WHERE name = %s",
            tuple(values),
        )
        frappe.db.commit()
        final_name = existing_name

    else:
        # ----------------------------------------------------------
        # NEW: ORM insert with all flags disabled
        # ----------------------------------------------------------
        doc = frappe.new_doc("Department")
        # Department uses department_name as its naming series title;
        # name == department_name in most ERPNext setups.
        doc.name = department_name
        doc.flags.name_set = True

        update_dict = {
            "department_name": department_name,
            "parent_department": parent_department,
            "company": company,
            "is_group": is_group,
            "disabled": disabled,
        }
        if has_remote_id:
            update_dict["remote_id"] = src.remote_id
        if has_source_site:
            update_dict["source_site"] = source_site
        doc.update(update_dict)

        doc.flags.ignore_permissions = True
        doc.flags.ignore_validate = True
        doc.flags.ignore_links = True
        doc.flags.ignore_mandatory = True

        doc.save()
        frappe.db.commit()
        final_name = doc.name

    # ----------------------------------------------------------
    # Write back the Department name as remote_id on External Department
    # (so future syncs can find the local doc by remote_id)
    # ----------------------------------------------------------
    ext_meta = frappe.get_meta("External Department")
    if ext_meta.has_field("remote_id"):
        frappe.db.set_value(
            "External Department",
            src.name,
            "remote_id",
            final_name,
            update_modified=False,
        )
        frappe.db.commit()

    frappe.clear_document_cache("Department", final_name)
    return frappe.get_doc("Department", final_name)


# ==========================================================
# ENTRY POINTS
# ==========================================================

@frappe.whitelist()
def sync_department_docs(source_doctype: str, names):
    """
    Whitelisted entry point to queue department sync.
    Called from the list-view Sync button with selected External Department names.
    """
    if isinstance(names, str):
        names = json.loads(names)

    frappe.enqueue(
        "emkan_insights.emkan_insights.doctype.external_department.external_department.sync_bulk_departments",
        queue="long",
        names=names,
        timeout=2000,
    )
    return "Sync Queued"


def sync_bulk_departments(names):
    """
    Bulk sync departments from External Department to Department.
    Processed in background queue.
    """
    results = []

    for name in names:
        try:
            frappe.db.commit()
            src = frappe.get_doc("External Department", name)

            department_name = getattr(src, "department_name", None) or src.name
            existing = _find_existing_department(
                getattr(src, "remote_id", None),
                department_name,
            )

            doc = upsert_department(src, existing)

            results.append(
                {
                    "name": name,
                    "status": "success",
                    "department_name": doc.name,
                }
            )

            if len(results) % 10 == 0:
                frappe.db.commit()

        except Exception as e:
            import traceback

            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }
            )

    frappe.db.commit()
    return results
