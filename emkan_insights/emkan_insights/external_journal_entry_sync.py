import frappe
import json
from frappe.utils import flt, cint


def _resolve_reference_name(reference_type, reference_name, company_abbr=None):
    """
    Resolve JE Account.reference_name to the local linked doc.
    Same prefix pattern as SO/DN: try exact, then {abbr}-{name}.
    """
    if not reference_name:
        return None

    reference_name = str(reference_name).strip()
    if not reference_name:
        return None

    doctype = (reference_type or "").strip() or None
    candidates = [reference_name]
    if company_abbr and not reference_name.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{reference_name}")

    if doctype and frappe.db.exists("DocType", doctype):
        for name in candidates:
            if frappe.db.exists(doctype, name):
                return name
        return reference_name

    return reference_name


def _resolve_party(party_type, party, company_abbr=None):
    """
    Resolve JE Account.party to the local Customer / Supplier / Employee.
    Employee sync names are {abbr}-{remote_id} (e.g. 130040 → IMC-130040).
    """
    if not party:
        return None

    party = str(party).strip()
    if not party:
        return None

    party_type = (party_type or "").strip()
    if not party_type or not frappe.db.exists("DocType", party_type):
        return party

    # Employee: same resolution as external_employee.resolve_employee
    if party_type == "Employee":
        if company_abbr and not party.startswith(f"{company_abbr}-"):
            prefixed = f"{company_abbr}-{party}"
            if frappe.db.exists("Employee", prefixed):
                return prefixed
        if frappe.db.exists("Employee", party):
            return party
        for field in ("employee_number", "remote_id", "custom_remote_id"):
            if frappe.get_meta("Employee").has_field(field):
                found = frappe.db.get_value("Employee", {field: party}, "name")
                if found:
                    return found
        return party

    # Customer / Supplier / other party doctypes
    candidates = [party]
    if company_abbr and not party.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{party}")

    for name in candidates:
        if frappe.db.exists(party_type, name):
            return name

    meta = frappe.get_meta(party_type)
    for name in candidates:
        for field in ("remote_id", "custom_remote_id"):
            if meta.has_field(field):
                found = frappe.db.get_value(party_type, {field: name}, "name")
                if found:
                    return found

    return party


def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message or "")


def _set_docstatus_sql(doctype, docname, docstatus, status):
    frappe.db.sql("""
        UPDATE `tab{doctype}`
        SET docstatus = %s,
            status = %s,
            modified = NOW(),
            modified_by = SUBSTRING_INDEX(USER(), '@', 1)
        WHERE name = %s
    """.format(doctype=doctype), (docstatus, status, docname))

    meta = frappe.get_meta(doctype)
    for df in meta.get_table_fields():
        child_doctype = df.options
        if frappe.db.exists("DocType", child_doctype):
            frappe.db.sql("""
                UPDATE `tab{child_doctype}`
                SET docstatus = %s,
                    modified = NOW(),
                    modified_by = SUBSTRING_INDEX(USER(), '@', 1)
                WHERE parent = %s AND parenttype = %s
            """.format(child_doctype=child_doctype), (docstatus, docname, doctype))

    frappe.clear_document_cache(doctype, docname)


def _force_cancel_doc(doctype, docname):
    _set_docstatus_sql(doctype, docname, 2, "Cancelled")


def _force_submit_doc(doctype, docname):
    _set_docstatus_sql(doctype, docname, 1, "Submitted")


def _safe_submit_doc(doc, log_prefix=""):
    try:
        doc.flags.ignore_permissions = True
        doc.flags.ignore_validate = True
        doc.flags.ignore_mandatory = True
        doc.flags.ignore_links = True
        doc.submit()
        frappe.db.commit()
        return True
    except Exception as submit_err:
        current = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
        if current == 1:
            frappe.db.commit()
            return True
        _log(f"{log_prefix} normal submit failed, attempting force submit",
             f"Error: {str(submit_err)}")
        try:
            _force_submit_doc(doc.doctype, doc.name)
            frappe.db.commit()
            return True
        except Exception as force_submit_err:
            _log(f"{log_prefix} force submit also failed",
                 f"Normal: {str(submit_err)}\nForce: {str(force_submit_err)}")
            raise


def _safe_cancel_doc(doc, log_prefix=""):
    try:
        doc.flags.ignore_permissions = True
        doc.flags.ignore_validate = True
        doc.flags.ignore_mandatory = True
        doc.flags.ignore_links = True
        doc.cancel()
        frappe.db.commit()
        return True
    except Exception as normal_cancel_err:
        _log(f"{log_prefix} normal cancel failed, attempting force cancel",
             f"Error: {str(normal_cancel_err)}")
        try:
            _force_cancel_doc(doc.doctype, doc.name)
            frappe.db.commit()
            return True
        except Exception as force_cancel_err:
            _log(f"{log_prefix} force cancel also failed",
                 f"Normal: {str(normal_cancel_err)}\nForce: {str(force_cancel_err)}")
            raise


def _sync_existing_docstatus(existing_name, external):
    """Sync docstatus for already-existing JE (same pattern as Sales Order)."""
    local_docstatus = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
    target_docstatus = cint(external.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists", "docstatus": local_docstatus}

    try:
        je = frappe.get_doc("Journal Entry", existing_name)

        if target_docstatus == 2:
            if local_docstatus == 0:
                _safe_submit_doc(je, log_prefix=f"Journal Entry {existing_name}")
                je = frappe.get_doc("Journal Entry", existing_name)
            _safe_cancel_doc(je, log_prefix=f"Journal Entry {existing_name}")
            final = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
            return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}

        if target_docstatus == 1 and local_docstatus == 0:
            _safe_submit_doc(je, log_prefix=f"Journal Entry {existing_name}")
            final = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
            return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}

        return {
            "name": existing_name,
            "status": "exists",
            "warning": "unhandled transition",
            "docstatus": local_docstatus,
        }
    except Exception as e:
        final = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
        if final == target_docstatus:
            frappe.db.commit()
            return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}
        _log(f"Journal Entry {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(e)}


def balance_journal_entry(je):
    """
    Soft-balance JE if debit != credit.
    If Company has no default_expense_account, ignore (do not throw) —
    insert still proceeds with ignore_validate.
    """
    total_debit = sum(flt(d.debit) for d in je.accounts)
    total_credit = sum(flt(d.credit) for d in je.accounts)
    diff = round(total_debit - total_credit, 2)

    if abs(diff) < 0.01:
        return

    adjustment_account = frappe.db.get_value("Company", je.company, "default_expense_account")
    if not adjustment_account:
        # Ignore missing default expense account — do not block sync
        _log(
            f"Journal Entry {je.name or ''} balance ignored",
            f"Debit/Credit differ by {diff}; Company '{je.company}' has no "
            f"default_expense_account. Sync continues with ignore_validate.",
        )
        return

    if diff > 0:
        je.append("accounts", {
            "account": adjustment_account,
            "credit_in_account_currency": abs(diff),
            "credit": abs(diff),
            "debit_in_account_currency": 0,
            "debit": 0,
        })
    else:
        je.append("accounts", {
            "account": adjustment_account,
            "debit_in_account_currency": abs(diff),
            "debit": abs(diff),
            "credit_in_account_currency": 0,
            "credit": 0,
        })


def _build_account_rows(ext, company_abbr, company):
    account_totals = {}

    for row in ext.accounts:
        # Keep all account rows (do not drop Stock / other types)
        if not row.account:
            continue

        reference_type = row.get("reference_type") or None
        reference_name = _resolve_reference_name(
            reference_type, row.get("reference_name"), company_abbr
        )
        party_type = row.get("party_type") or None
        party = _resolve_party(party_type, row.get("party"), company_abbr)

        group_key = (
            row.account,
            reference_type or "",
            reference_name or "",
            party_type or "",
            party or "",
        )
        if group_key not in account_totals:
            account_totals[group_key] = {
                "account": row.account,
                "debit": 0.0,
                "credit": 0.0,
                "party_type": party_type,
                "party": party,
                "cost_center": row.cost_center or frappe.get_cached_value("Company", company, "cost_center"),
                "reference_type": reference_type,
                "reference_name": reference_name,
            }

        account_totals[group_key]["debit"] += flt(row.debit_in_account_currency)
        account_totals[group_key]["credit"] += flt(row.credit_in_account_currency)

    final_rows = []
    for vals in account_totals.values():
        net_amount = flt(vals["debit"] - vals["credit"], 2)
        if net_amount == 0:
            continue

        final_rows.append({
            "account": vals["account"],
            "party_type": vals["party_type"],
            "party": vals["party"],
            "cost_center": vals["cost_center"],
            "reference_type": vals["reference_type"],
            "reference_name": vals["reference_name"],
            "debit_in_account_currency": net_amount if net_amount > 0 else 0,
            "credit_in_account_currency": abs(net_amount) if net_amount < 0 else 0,
            "debit": net_amount if net_amount > 0 else 0,
            "credit": abs(net_amount) if net_amount < 0 else 0,
        })

    return final_rows


@frappe.whitelist()
def sync_external_journal_entries(names):
    if isinstance(names, str):
        names = json.loads(names)

    result = {"created": [], "skipped": [], "failed": []}

    for name in names:
        try:
            ext = frappe.get_doc("External Journal Entry", name)
            remote_id = ext.name

            company_abbr = frappe.db.get_value("Company", ext.company, "abbr") or ""
            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # Existing by remote_id or target name — still sync docstatus
            existing = None
            if frappe.get_meta("Journal Entry").has_field("remote_id"):
                existing = frappe.db.get_value(
                    "Journal Entry", {"remote_id": remote_id}, "name"
                )
            if not existing:
                existing = (
                    target_name
                    if frappe.db.exists("Journal Entry", target_name)
                    else None
                )
            if not existing and frappe.db.exists("Journal Entry", remote_id):
                existing = remote_id

            if existing:
                sync_result = _sync_existing_docstatus(existing, ext)
                if sync_result.get("status") == "docstatus_synced":
                    result["created"].append(sync_result)
                else:
                    result["skipped"].append({
                        "external": name,
                        "reason": sync_result.get("warning") or sync_result.get("error") or "Already Synced",
                        "name": existing,
                        "docstatus": sync_result.get("docstatus"),
                    })
                continue

            # CREATE NEW JE (draft / submitted / cancelled all allowed)
            je = frappe.new_doc("Journal Entry")
            je.name = target_name
            je.flags.name_set = True
            je.flags.ignore_naming_series = True
            if frappe.get_meta("Journal Entry").has_field("remote_id"):
                je.remote_id = remote_id

            je.voucher_type = ext.voucher_type or "Journal Entry"
            je.posting_date = ext.posting_date
            je.company = ext.company
            je.remark = f"Synced from {ext.name}"

            final_rows = _build_account_rows(ext, company_abbr, ext.company)
            if not final_rows:
                result["failed"].append({"external": name, "error": "Zero balance rows"})
                continue

            je.set("accounts", final_rows)
            balance_journal_entry(je)
            je.set("accounts", [r for r in je.accounts if flt(r.debit) > 0 or flt(r.credit) > 0])

            je.total_debit = sum(flt(r.debit) for r in je.accounts)
            je.total_credit = sum(flt(r.credit) for r in je.accounts)

            je.flags.ignore_permissions = True
            je.flags.ignore_link_validation = True
            je.flags.ignore_validate = True
            je.flags.ignore_links = True
            je.flags.ignore_naming_series = True
            je.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)

            if je.name != target_name:
                if not frappe.db.exists("Journal Entry", target_name):
                    frappe.db.sql("""
                        UPDATE `tabJournal Entry`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, je.name))
                    old_name = je.name
                    frappe.db.sql("""
                        UPDATE `tabJournal Entry Account`
                        SET parent = %s
                        WHERE parent = %s
                    """, (target_name, old_name))
                    je.name = target_name

            if frappe.get_meta("Journal Entry").has_field("remote_id"):
                frappe.db.set_value(
                    "Journal Entry", je.name, "remote_id", remote_id, update_modified=False
                )

            frappe.db.commit()

            # DOCSTATUS SYNC (same as Sales Order / Stock Entry)
            target_status = cint(ext.docstatus)
            frappe.clear_document_cache("Journal Entry", je.name)
            je = frappe.get_doc("Journal Entry", je.name)

            if target_status == 1:
                _safe_submit_doc(je, log_prefix=f"Journal Entry {je.name}")
            elif target_status == 2:
                _safe_submit_doc(je, log_prefix=f"Journal Entry {je.name}")
                je = frappe.get_doc("Journal Entry", je.name)
                _safe_cancel_doc(je, log_prefix=f"Journal Entry {je.name}")
            # target_status == 0 → leave as Draft

            final_status = cint(frappe.db.get_value("Journal Entry", je.name, "docstatus"))
            result["created"].append({"name": je.name, "docstatus": final_status})
            frappe.db.commit()

        except Exception as e:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"Sync Error: {name}")
            result["failed"].append({"external": name, "error": str(e)})

    return result
