# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs

class ExternalEmployee(Document):
    pass

import frappe
import json
from frappe.utils import flt, nowdate, getdate
from emkan_insights.emkan_insights.external_sync_utils import log_record_failure


# ==========================================================
# HELPERS
# ==========================================================

def resolve_department(department_name, company=None):
    """Resolve a department name to a valid department in this company."""
    if not department_name:
        return None

    if frappe.db.exists("Department", department_name):
        return department_name

    # Try to find by department_name field
    found = frappe.db.get_value("Department", {"department_name": department_name}, "name")
    if found:
        return found

    # Try partial match (some departments have suffix like " - IMC")
    found = frappe.db.sql(
        "SELECT name FROM `tabDepartment` WHERE name LIKE %s LIMIT 1",
        (f"%{department_name}%",),
        as_dict=False
    )
    if found:
        return found[0][0]

    return None


def resolve_designation(designation_name):
    """Resolve a designation name to a valid designation."""
    if not designation_name:
        return None

    if frappe.db.exists("Designation", designation_name):
        return designation_name

    found = frappe.db.get_value("Designation", {"designation_name": designation_name}, "name")
    if found:
        return found

    return None


def resolve_branch(branch_name, company=None):
    """Resolve a branch name to a valid branch."""
    if not branch_name:
        return None

    if frappe.db.exists("Branch", branch_name):
        return branch_name

    return None


def resolve_employee(reports_to_name, company_abbr=None):
    """Resolve a reports_to employee name to a valid employee."""
    if not reports_to_name:
        return None

    # Try prefixed name
    if company_abbr and not reports_to_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{reports_to_name}"
        if frappe.db.exists("Employee", prefixed):
            return prefixed

    # Try direct lookup
    if frappe.db.exists("Employee", reports_to_name):
        return reports_to_name

    # Try by employee_number
    found = frappe.db.get_value("Employee", {"employee_number": reports_to_name}, "name")
    if found:
        return found

    # Try by user_id
    found = frappe.db.get_value("Employee", {"user_id": reports_to_name}, "name")
    if found:
        return found

    # Try by remote_id
    found = frappe.db.get_value("Employee", {"remote_id": reports_to_name}, "name")
    if found:
        return found

    return None


def resolve_holiday_list(holiday_list_name):
    """Resolve a holiday list name."""
    if not holiday_list_name:
        return None

    if frappe.db.exists("Holiday List", holiday_list_name):
        return holiday_list_name

    return None


def _resolve_link_with_abbr(doctype, remote_name, company_abbr=None):
    """Generic link resolution with company abbreviation prefix support."""
    if not remote_name:
        return None

    if company_abbr and remote_name.startswith(f"{company_abbr}-"):
        return remote_name

    if company_abbr:
        prefixed = f"{company_abbr}-{remote_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    meta = frappe.get_meta(doctype)
    for field in ("remote_id", "custom_remote_id"):
        if meta.has_field(field):
            local_name = frappe.db.get_value(doctype, {field: remote_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_name):
        return remote_name

    return None


def _build_target_name(company_abbr, remote_id):
    """Build the target document name with company abbreviation prefix."""
    if company_abbr and remote_id and remote_id.startswith(f"{company_abbr}-"):
        return remote_id
    return f"{company_abbr}-{remote_id}" if company_abbr and remote_id else remote_id


def _find_existing_employee(remote_id, target_name):
    """
    Check by remote_id first (authoritative link back to source),
    then fall back to the prefixed target name, then by employee_number.
    """
    if remote_id:
        existing = frappe.db.get_value("Employee", {"remote_id": remote_id}, "name")
        if existing:
            return existing

    if target_name and frappe.db.exists("Employee", target_name):
        return target_name

    # Fallback: check by employee_number if remote_id looks like an employee number
    if remote_id:
        existing = frappe.db.get_value("Employee", {"employee_number": remote_id}, "name")
        if existing:
            return existing

    return None


def sync_docstatus(doc, target_status):
    """
    Forcefully sets docstatus to match the source using direct DB writes.
    Note: Employee doesn't typically use docstatus (always 0), but
    keeping for pattern consistency.
    """
    target = int(target_status)
    if doc.docstatus == target:
        return

    _force_docstatus_db(doc.name, doc.doctype, target)
    doc.docstatus = target


def _force_docstatus_db(name, doctype, target_docstatus):
    """Directly updates docstatus without triggering hooks."""
    frappe.db.set_value(doctype, name, "docstatus", target_docstatus, update_modified=False)

    child_meta = frappe.get_meta(doctype)
    for df in child_meta.get_table_fields():
        child_doctype = df.options
        if frappe.db.table_exists(f"tab{child_doctype}"):
            frappe.db.sql(
                f"""UPDATE `tab{child_doctype}`
                    SET docstatus = %s
                    WHERE parent = %s AND parenttype = %s""",
                (target_docstatus, name, doctype)
            )


# ==========================================================
# SAFE CHILD TABLE INSERTIONS
# ==========================================================

def _insert_education_safely(employee_name, src):
    """
    Insert Employee Education records safely.
    Dedup uses the SOURCE ROW'S identity (row.name / custom_remote_id).
    """
    if not getattr(src, "education", None):
        return 0, []

    success_count = 0
    errors = []
    seen_rows = set()

    for idx, row in enumerate(src.education, start=1):
        try:
            row_identifier = getattr(row, "custom_remote_id", None) or row.name
            if row_identifier in seen_rows:
                frappe.log_error(
                    title=f"Employee {employee_name} duplicate education skipped",
                    message=f"Source row {row_identifier} (idx {idx}) already processed."
                )
                continue
            seen_rows.add(row_identifier)

            edu_row = {
                "doctype": "Employee Education",
                "name": frappe.generate_hash(length=10),
                "parent": employee_name,
                "parentfield": "education",
                "parenttype": "Employee",
                "idx": idx,
                "school_or_university": (
                    getattr(row, "school_or_university", None)
                    or getattr(row, "school_university", None)
                ),
                "qualification": getattr(row, "qualification", None),
                "level": getattr(row, "level", None),
                "year_of_passing": getattr(row, "year_of_passing", None),
                "class_or_percentage": (
                    getattr(row, "class_or_percentage", None)
                    or getattr(row, "class_percentage", None)
                ),
                "major_or_optional_subjects": (
                    getattr(row, "major_or_optional_subjects", None)
                    or getattr(row, "major_optional_subjects", None)
                ),
                "custom_remote_id": getattr(row, "custom_remote_id", None) or row.name,
            }

            child_doc = frappe.get_doc(edu_row)
            child_doc.db_insert()
            success_count += 1

        except Exception as e:
            error_msg = f"Education {idx} (row: {row.name}): {str(e)}"
            errors.append(error_msg)
            frappe.log_error(
                title=f"Employee {employee_name} education insert failed",
                message=error_msg
            )

    
    return success_count, errors


def _insert_external_work_history_safely(employee_name, src, company_abbr=None):
    """
    Insert External Work History records safely.
    Dedup uses the SOURCE ROW'S identity.
    """
    if not getattr(src, "external_work_history", None):
        return 0, []

    success_count = 0
    errors = []
    seen_rows = set()

    for idx, row in enumerate(src.external_work_history, start=1):
        try:
            row_identifier = getattr(row, "custom_remote_id", None) or row.name
            if row_identifier in seen_rows:
                frappe.log_error(
                    title=f"Employee {employee_name} duplicate external work history skipped",
                    message=f"Source row {row_identifier} (idx {idx}) already processed."
                )
                continue
            seen_rows.add(row_identifier)

            hist_row = {
                "doctype": "External Work History",
                "name": frappe.generate_hash(length=10),
                "parent": employee_name,
                "parentfield": "external_work_history",
                "parenttype": "Employee",
                "idx": idx,
                "company_name": (
                    getattr(row, "company_name", None)
                    or getattr(row, "company", None)
                ),
                "designation": getattr(row, "designation", None),
                "from_date": getattr(row, "from_date", None),
                "to_date": getattr(row, "to_date", None),
                "total_experience": flt(getattr(row, "total_experience", 0)),
                "address": getattr(row, "address", None),
                "contact": getattr(row, "contact", None),
                "salary": flt(getattr(row, "salary", 0)),
                "custom_remote_id": getattr(row, "custom_remote_id", None) or row.name,
            }

            child_doc = frappe.get_doc(hist_row)
            child_doc.db_insert()
            success_count += 1

        except Exception as e:
            error_msg = f"External Work History {idx} (row: {row.name}): {str(e)}"
            errors.append(error_msg)
            frappe.log_error(
                title=f"Employee {employee_name} external work history insert failed",
                message=error_msg
            )

    
    return success_count, errors


def _insert_internal_work_history_safely(employee_name, src, company_abbr=None):
    """
    Insert Internal Work History records safely.
    Dedup uses the SOURCE ROW'S identity.
    Resolves department, designation, branch to local equivalents.
    """
    if not getattr(src, "internal_work_history", None):
        return 0, []

    success_count = 0
    errors = []
    seen_rows = set()

    for idx, row in enumerate(src.internal_work_history, start=1):
        try:
            row_identifier = getattr(row, "custom_remote_id", None) or row.name
            if row_identifier in seen_rows:
                frappe.log_error(
                    title=f"Employee {employee_name} duplicate internal work history skipped",
                    message=f"Source row {row_identifier} (idx {idx}) already processed."
                )
                continue
            seen_rows.add(row_identifier)

            hist_row = {
                "doctype": "Internal Work History",
                "name": frappe.generate_hash(length=10),
                "parent": employee_name,
                "parentfield": "internal_work_history",
                "parenttype": "Employee",
                "idx": idx,
                "department": resolve_department(getattr(row, "department", None)),
                "designation": resolve_designation(getattr(row, "designation", None)),
                "branch": resolve_branch(getattr(row, "branch", None)),
                "from_date": getattr(row, "from_date", None),
                "to_date": getattr(row, "to_date", None),
                "custom_remote_id": getattr(row, "custom_remote_id", None) or row.name,
            }

            child_doc = frappe.get_doc(hist_row)
            child_doc.db_insert()
            success_count += 1

        except Exception as e:
            error_msg = f"Internal Work History {idx} (row: {row.name}): {str(e)}"
            errors.append(error_msg)
            frappe.log_error(
                title=f"Employee {employee_name} internal work history insert failed",
                message=error_msg
            )

    
    return success_count, errors


# ==========================================================
# MAIN UPSERT
# ==========================================================

def upsert_employee(src, existing_name):
    """
    Upsert an Employee record from an External Employee source.
    
    Both existing and new paths now use ORM doc.update() + doc.save()
    to ensure 100% field parity and zero explicit commits.
    """
    company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""
    target_name = _build_target_name(company_abbr, src.remote_id or getattr(src, "employee", None) or src.name)

    # Resolve linked documents
    department = resolve_department(getattr(src, "department", None), src.company)
    designation = resolve_designation(getattr(src, "designation", None))
    branch = resolve_branch(getattr(src, "branch", None), src.company)
    reports_to = resolve_employee(getattr(src, "reports_to", None), company_abbr)
    holiday_list = resolve_holiday_list(getattr(src, "holiday_list", None))

    # Map status
    status_map = {
        "Active": "Active",
        "Inactive": "Inactive",
        "Left": "Left",
        "Court Case": "Court Case",
        "Suspended": "Suspended",
    }
    employee_status = status_map.get(getattr(src, "status", "Active"), "Active")

    # Build employee_name from components if not directly available
    employee_name_value = (
        getattr(src, "employee_name", None)
        or getattr(src, "full_name", None)
    )
    if not employee_name_value:
        parts = []
        if getattr(src, "salutation", None):
            parts.append(src.salutation)
        if getattr(src, "first_name", None):
            parts.append(src.first_name)
        if getattr(src, "middle_name", None):
            parts.append(src.middle_name)
        if getattr(src, "last_name", None):
            parts.append(src.last_name)
        employee_name_value = " ".join(parts).strip() if parts else None

    # ----------------------------------------------------------
    # Unified field dictionary (used for BOTH new and existing)
    # ----------------------------------------------------------
    source_site_value = getattr(src, "source_site", None) or getattr(src, "site_url", None)

    update_dict = {
        "employee_name": employee_name_value,
        "first_name": getattr(src, "first_name", None),
        "middle_name": getattr(src, "middle_name", None),
        "last_name": getattr(src, "last_name", None),
        "gender": getattr(src, "gender", None),
        "date_of_birth": getattr(src, "date_of_birth", None),
        "date_of_joining": getattr(src, "date_of_joining", None) or nowdate(),
        "status": employee_status,
        "company": src.company,
        "department": department,
        "designation": designation,
        "reports_to": reports_to,
        "branch": branch,
        "employee_number": getattr(src, "employee_number", None),
        "salutation": getattr(src, "salutation", None),
        "image": getattr(src, "image", None),
        "user_id": getattr(src, "user_id", None),
        "create_user_permission": int(getattr(src, "create_user_permission", 0) or 0),
        "offer_date": getattr(src, "offer_date", None),
        "confirmation_date": getattr(src, "confirmation_date", None),
        "contract_end_date": getattr(src, "contract_end_date", None),
        "notice_days": int(getattr(src, "notice_days", 0) or 0),
        "date_of_retirement": getattr(src, "date_of_retirement", None),
        "cell_number": getattr(src, "cell_number", None) or getattr(src, "mobile", None),
        "personal_email": getattr(src, "personal_email", None),
        "company_email": getattr(src, "company_email", None),
        "prefered_contact_email": getattr(src, "prefered_contact_email", None),
        "prefered_email": getattr(src, "prefered_email", None),
        "unsubscribed": int(getattr(src, "unsubscribed", 0) or 0),
        "current_address": getattr(src, "current_address", None),
        "current_address_is": getattr(src, "current_address_is", None),
        "permanent_address": getattr(src, "permanent_address", None),
        "permanent_address_is": getattr(src, "permanent_address_is", None),
        "emergency_contact_name": getattr(src, "emergency_contact_name", None),
        "emergency_phone": getattr(src, "emergency_phone", None),
        "relation": getattr(src, "relation", None),
        "attendance_device_id": getattr(src, "attendance_device_id", None),
        "holiday_list": holiday_list,
        "cost_to_company": flt(getattr(src, "cost_to_company", 0)),
        "salary_currency": getattr(src, "salary_currency", None) or "SAR",
        "salary_mode": getattr(src, "salary_mode", None),
        "bank_name": getattr(src, "bank_name", None),
        "bank_ac_no": getattr(src, "bank_ac_no", None),
        "iban": getattr(src, "iban", None),
        "marital_status": getattr(src, "marital_status", None),
        "blood_group": getattr(src, "blood_group", None),
        "health_details": getattr(src, "health_details", None),
        "passport_number": getattr(src, "passport_number", None),
        "valid_upto": getattr(src, "valid_upto", None),
        "date_of_issue": getattr(src, "date_of_issue", None),
        "place_of_issue": getattr(src, "place_of_issue", None),
        "bio": getattr(src, "bio", None) or getattr(src, "cover_letter", None),
        "resignation_letter_date": getattr(src, "resignation_letter_date", None),
        "relieving_date": getattr(src, "relieving_date", None),
        "exit_interview_held_on": getattr(src, "exit_interview_held_on", None),
        "new_workplace": getattr(src, "new_workplace", None),
        "leave_encashed": int(getattr(src, "leave_encashed", 0) or 0),
        "encashment_date": getattr(src, "encashment_date", None),
        "reason_for_leaving": getattr(src, "reason_for_leaving", None),
        "feedback": getattr(src, "feedback", None),
        "remote_id": src.remote_id,
        "source_site": source_site_value,
    }

    # ----------------------------------------------------------
    # EXISTING: Update via ORM (doc.save)
    # ----------------------------------------------------------
    if existing_name:
        doc = frappe.get_doc("Employee", existing_name)
        
        doc.flags.ignore_permissions = True

        # Delete existing child records using raw SQL to preserve the exact
        # original filtering behavior (Education omits parenttype filter)
        frappe.db.sql("DELETE FROM `tabEmployee Education` WHERE parent = %s", (existing_name,))
        if frappe.db.table_exists("tabExternal Work History"):
            frappe.db.sql(
                "DELETE FROM `tabExternal Work History` WHERE parent = %s AND parenttype = 'Employee'",
                (existing_name,)
            )
        if frappe.db.table_exists("tabInternal Work History"):
            frappe.db.sql(
                "DELETE FROM `tabInternal Work History` WHERE parent = %s AND parenttype = 'Employee'",
                (existing_name,)
            )
        

        doc.update(update_dict)
        doc.save()  # ORM save — generates the UPDATE query dynamically
        employee_name = doc.name

    else:
        # ----------------------------------------------------------
        # NEW: ORM insert with all flags disabled
        # ----------------------------------------------------------
        doc = frappe.new_doc("Employee")
        doc.name = target_name
        doc.flags.name_set = True

        doc.update(update_dict)

        doc.flags.ignore_permissions = True

        doc.set("education", [])
        if hasattr(doc, "external_work_history"):
            doc.set("external_work_history", [])
        if hasattr(doc, "internal_work_history"):
            doc.set("internal_work_history", [])

        doc.save()  # ORM save — generates the INSERT query
        employee_name = doc.name

    # ----------------------------------------------------------
    # Insert children
    # ----------------------------------------------------------
    edu_success, edu_errors = _insert_education_safely(employee_name, src)
    if edu_errors:
        frappe.log_error(
            title=f"Employee {employee_name} - {len(edu_errors)} education errors",
            message="\n".join(edu_errors)
        )

    ext_success, ext_errors = _insert_external_work_history_safely(employee_name, src, company_abbr)
    if ext_errors:
        frappe.log_error(
            title=f"Employee {employee_name} - {len(ext_errors)} external work history errors",
            message="\n".join(ext_errors)
        )

    int_success, int_errors = _insert_internal_work_history_safely(employee_name, src, company_abbr)
    if int_errors:
        frappe.log_error(
            title=f"Employee {employee_name} - {len(int_errors)} internal work history errors",
            message="\n".join(int_errors)
        )

    # ----------------------------------------------------------
    # Write back remote_id onto the External Employee parent
    # ----------------------------------------------------------
    ext_emp_meta = frappe.get_meta("External Employee")
    if ext_emp_meta.has_field("remote_id"):
        src.db_set("remote_id", employee_name, update_modified=False)

    frappe.clear_document_cache("Employee", employee_name)
    return doc


# ==========================================================
# ENTRY POINTS
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_employee_docs(source_doctype: str, names, sync_now=False):
    """
    Sync External Employee records to the Employee doctype.

    When called from the sequential pipeline (sync_now=True), runs synchronously.
    When called from the client, enqueues a background job.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if sync_now:
        return sync_bulk_employees(names)

    frappe.enqueue(
        "emkan_insights.emkan_insights.doctype.external_employee.external_employee.sync_bulk_employees",
        queue="long",
        names=names,
        timeout=2000,
    )
    return "Sync Queued"


def sync_bulk_employees(names):
    """
    Bulk sync employees from External Employee to Employee.
    Processed in background queue.

    All database writes happen within a single transaction.
    Frappe automatically commits the transaction when the background job finishes.
    """
    results = []

    for name in names:
        try:
            src = frappe.get_doc("External Employee", name)

            company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""
            target_name = _build_target_name(
                company_abbr,
                src.remote_id or getattr(src, "employee", None) or src.name
            )
            existing = _find_existing_employee(
                src.remote_id or getattr(src, "employee", None),
                target_name
            )

            doc = upsert_employee(src, existing)

            results.append({
                "name": name,
                "status": "success",
                "employee_name": doc.name,
            })

        except Exception as e:
            log_record_failure("Employee", name, str(e))
            raise

    return results
