import frappe
import json
from frappe.utils import flt, cint


def _resolve_reference_name(reference_type, reference_name, company_abbr=None):
    """
    Resolve JE Account.reference_name to the local linked doc.
    Same prefix pattern as SO/DN/PE: try exact, then {abbr}-{name},
    then remote_id / custom_remote_id lookup (Payment Entry names often
    differ from the remote voucher id until prefixed locally).
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

        meta = frappe.get_meta(doctype)
        for name in candidates:
            for field in ("remote_id", "custom_remote_id"):
                if meta.has_field(field):
                    found = frappe.db.get_value(doctype, {field: name}, "name")
                    if found:
                        return found

        # Do not keep a dangling reference — it causes "Could not find
        # Against Voucher" warnings/errors during GL posting.
        _log(
            f"JE reference unresolved: {doctype} {reference_name}",
            f"Tried candidates {candidates}; no local document found. "
            f"Reference cleared so submit can proceed. Re-sync after the "
            f"linked {doctype} exists locally if the link is required.",
        )
        return None

    return reference_name


def _resolve_cost_center(cost_center, company, company_abbr, cc_cache):
    """
    Resolve a cost center from the source document to a valid local one.
    Mirrors external_si_sync._resolve_cost_center.
    """
    if not cost_center:
        return None

    if cost_center in cc_cache:
        return cc_cache[cost_center]

    result = None
    single_suffix = f" - {company_abbr}" if company_abbr else ""
    double_suffix = f"{single_suffix}{single_suffix}" if company_abbr else ""

    if frappe.db.exists("Cost Center", cost_center):
        result = cost_center

    if not result and double_suffix and cost_center.endswith(double_suffix):
        candidate = cost_center[: -len(double_suffix)] + single_suffix
        if frappe.db.exists("Cost Center", candidate):
            result = candidate

    if not result and single_suffix and cost_center.endswith(single_suffix):
        candidate = cost_center[: -len(single_suffix)]
        if frappe.db.exists("Cost Center", candidate):
            result = candidate

    if not result and single_suffix and not cost_center.endswith(single_suffix):
        candidate = cost_center + single_suffix
        if frappe.db.exists("Cost Center", candidate):
            result = candidate

    if not result and company:
        default_cc = frappe.db.get_value("Company", company, "cost_center")
        if default_cc and frappe.db.exists("Cost Center", default_cc):
            result = default_cc

    cc_cache[cost_center] = result
    return result


def _get_fallback_cost_center(company, company_abbr, cc_cache):
    """Last-resort cost center when source row and Company default are empty."""
    cache_key = f"__fallback__{company}"
    if cache_key in cc_cache:
        return cc_cache[cache_key]

    default_cc = frappe.db.get_value("Company", company, "cost_center")
    resolved = _resolve_cost_center(default_cc, company, company_abbr, cc_cache) if default_cc else None
    if not resolved:
        resolved = frappe.db.get_value(
            "Cost Center",
            {"company": company, "is_group": 0, "disabled": 0},
            "name",
            order_by="creation asc",
        )

    cc_cache[cache_key] = resolved
    return resolved


def _cost_center_for_account(account, cost_center, company, company_abbr, cc_cache):
    """Ensure P&L account rows always carry a valid local cost center."""
    resolved = _resolve_cost_center(cost_center, company, company_abbr, cc_cache)
    if resolved:
        return resolved

    if account and frappe.get_cached_value("Account", account, "report_type") == "Profit and Loss":
        return _get_fallback_cost_center(company, company_abbr, cc_cache)

    return resolved


def _resolve_party(party_type, party, company_abbr=None):
    """
    Resolve JE Account.party to the local Customer / Supplier / Employee.
    Employee sync names are {abbr}-{remote_id} (e.g. 130040 -> IMC-130040).
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


def _apply_submit_flags(doc):
    doc.flags.ignore_permissions = True


def _safe_submit_doc(doc, log_prefix=""):
    """
    Submit synchronously via _submit(), NOT submit().

    ERPNext's JournalEntry.submit() queues background submission when a JE
    has more than 100 account rows. That returns without error but leaves
    docstatus at 0 — exactly why payroll JEs (160-175 rows) were stuck as
    Draft with zero GL entries after sync reported success.
    """
    current_docstatus = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
    if current_docstatus == 1:
        return True

    _apply_submit_flags(doc)
    try:
        doc._submit()
    except Exception as submit_err:
        if cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus")) == 1:
            return True
        raise submit_err

    final_docstatus = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
    if final_docstatus != 1:
        raise frappe.ValidationError(
            f"{log_prefix} submit did not reach docstatus=1 (still {final_docstatus}). "
            f"Large Journal Entries must be submitted synchronously during sync."
        )
    return True


def _safe_cancel_doc(doc, log_prefix=""):
    current_docstatus = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
    if current_docstatus == 2:
        return True

    _apply_submit_flags(doc)
    try:
        # cancel() also queues when accounts > 100 — use _cancel() for sync.
        if len(getattr(doc, "accounts", []) or []) > 100:
            doc._cancel()
        else:
            doc.cancel()
    except Exception as cancel_err:
        if cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus")) == 2:
            return True
        raise cancel_err
    return True


def _sync_existing_docstatus(existing_name, external, company_abbr=None, company=None, cc_cache=None):
    """
    Sync docstatus for already-existing JE.

    IMPORTANT FIX: this used to catch all exceptions internally and return
    an "exists"/"error" dict, which meant a failed submit/cancel here never
    propagated to the caller's error handling - so no rollback ever
    happened for this path, and any GL Entries partially written by a
    failed on_submit were left dangling. Exceptions are now allowed to
    bubble up so the caller's per-record savepoint can clean up properly.
    """
    cc_cache = cc_cache if cc_cache is not None else {}
    local_docstatus = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
    target_docstatus = cint(external.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists", "docstatus": local_docstatus}

    je = frappe.get_doc("Journal Entry", existing_name)

    if local_docstatus == 0 and company:
        _repair_draft_je_from_external(je, external, company_abbr, company, cc_cache)
        je.reload()

    if target_docstatus == 2:  # Target is Cancelled
        if local_docstatus == 0:  # If currently Draft, submit first then cancel
            _safe_submit_doc(je, log_prefix=f"Journal Entry {existing_name}")
            je.reload()
        _safe_cancel_doc(je, log_prefix=f"Journal Entry {existing_name}")
        final = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
        return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}

    if target_docstatus == 1 and local_docstatus == 0:  # Target is Submitted, currently Draft
        _safe_submit_doc(je, log_prefix=f"Journal Entry {existing_name}")
        final = cint(frappe.db.get_value("Journal Entry", existing_name, "docstatus"))
        return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}

    return {
        "name": existing_name,
        "status": "exists",
        "warning": "unhandled transition",
        "docstatus": local_docstatus,
    }




def _build_account_rows(ext, company_abbr, company, cc_cache=None):
    """
    Copy each account row from the external JE 1-to-1.
    No grouping and no net-zero filtering - mirrors the PR sync row copy.
    """
    cc_cache = cc_cache if cc_cache is not None else {}
    final_rows = []
    skipped_rows_count = 0

    for idx, row in enumerate(ext.accounts):
        if not row.account:
            skipped_rows_count += 1
            _log(
                f"Journal Entry {ext.name} account row skipped",
                f"Row {idx + 1} was skipped because 'account' field was empty."
            )
            continue

        reference_type = row.get("reference_type") or None
        reference_name = _resolve_reference_name(
            reference_type, row.get("reference_name"), company_abbr
        )
        if reference_name is None:
            reference_type = None
        party_type = row.get("party_type") or None
        party = _resolve_party(party_type, row.get("party"), company_abbr)
        cost_center = _cost_center_for_account(
            row.account, row.cost_center, company, company_abbr, cc_cache
        )

        final_rows.append({
            "account": row.account,
            "party_type": party_type,
            "party": party,
            "cost_center": cost_center,
            "reference_type": reference_type,
            "reference_name": reference_name,
            "debit_in_account_currency": flt(row.debit_in_account_currency),
            "credit_in_account_currency": flt(row.credit_in_account_currency),
            "debit": flt(row.debit_in_account_currency),
            "credit": flt(row.credit_in_account_currency),
        })

    if skipped_rows_count > 0:
        _log(
            f"Journal Entry {ext.name} skipped {skipped_rows_count} account rows",
            f"Total {skipped_rows_count} rows were skipped due to empty 'account' field."
        )

    return final_rows


def _repair_draft_je_from_external(je, ext, company_abbr, company, cc_cache):
    """
    Re-apply cost-center and reference resolution on an existing Draft JE
    before submit. Needed when the JE was first synced before linked
    Payment Entries existed locally, or before cost-center mapping was fixed.
  Also fixes auto-balance plug rows that were appended without cost_center.
    """
    ext_rows = [r for r in ext.accounts if r.account]
    changed = False

    for idx, je_row in enumerate(je.accounts):
        ext_row = ext_rows[idx] if idx < len(ext_rows) else None

        if ext_row:
            reference_type = ext_row.get("reference_type") or None
            reference_name = _resolve_reference_name(
                reference_type, ext_row.get("reference_name"), company_abbr
            )
            if reference_name is None:
                reference_type = None
            source_cc = ext_row.cost_center
        else:
            reference_type = je_row.reference_type
            reference_name = je_row.reference_name
            if reference_type and reference_name:
                resolved_ref = _resolve_reference_name(
                    reference_type, reference_name, company_abbr
                )
                if resolved_ref is None:
                    reference_type = None
                    reference_name = None
                else:
                    reference_name = resolved_ref
            source_cc = je_row.cost_center

        cost_center = _cost_center_for_account(
            je_row.account, source_cc, company, company_abbr, cc_cache
        )

        if je_row.reference_type != reference_type:
            je_row.reference_type = reference_type
            changed = True
        if je_row.reference_name != reference_name:
            je_row.reference_name = reference_name
            changed = True
        if je_row.cost_center != cost_center:
            je_row.cost_center = cost_center
            changed = True

    if changed:
        _apply_submit_flags(je)
        je.save(ignore_permissions=True)
    return changed


@frappe.whitelist()
def sync_external_journal_entries(names):
    if isinstance(names, str):
        names = json.loads(names)

    result = {"created": [], "skipped": [], "failed": []}
    cc_cache = {}

    for name in names:
        # FIX: per-record savepoint. On any failure anywhere in this
        # record's processing (insert, rename SQL, submit, cancel), we
        # roll back ONLY this record's writes - including any GL Entries
        # ERPNext may have already written in on_submit before the
        # failure occurred. This is what was previously missing: the old
        # code explicitly avoided any rollback (to protect the insert),
        # which meant a failed submit could leave orphaned GL Entries
        # behind permanently, and repeated retries compounded them.
        # Using a savepoint scoped to just this iteration gives us the
        # safety of a rollback without touching any other record already
        # committed earlier in the same batch.
        savepoint = f"sync_je_{frappe.generate_hash(txt=name, length=10)}"
        frappe.db.savepoint(savepoint)

        try:
            ext = frappe.get_doc("External Journal Entry", name)
            remote_id = ext.name

            company_abbr = frappe.db.get_value("Company", ext.company, "abbr") or ""
            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # Existing by remote_id or target name - still sync docstatus
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
                sync_result = _sync_existing_docstatus(
                    existing, ext, company_abbr, ext.company, cc_cache
                )
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
            je.multi_currency = ext.multi_currency
            je.flags.name_set = True
            je.flags.ignore_naming_series = True
            if frappe.get_meta("Journal Entry").has_field("remote_id"):
                je.remote_id = remote_id

            je.voucher_type = ext.voucher_type or "Journal Entry"
            je.posting_date = ext.posting_date
            je.company = ext.company
            je.remark = f"Synced from {ext.name}"

            final_rows = _build_account_rows(ext, company_abbr, ext.company, cc_cache)
            if not final_rows:
                _log(
                    f"Journal Entry {name} has no valid account rows",
                    "All rows were skipped because 'account' field was empty.",
                )
                result["failed"].append({"external": name, "error": "No valid account rows (account field missing on all rows)"})
                frappe.db.rollback(save_point=savepoint)
                continue

            je.set("accounts", final_rows)
            # FIX: this now raises (instead of silently returning) when the
            # JE is unbalanced and no plug account is configured, so we
            # never insert/submit a broken entry.

            je.total_debit = ext.total_debit
            je.total_credit = ext.total_credit

            je.flags.ignore_permissions = True
            je.flags.ignore_naming_series = True
            je.insert(ignore_permissions=True)

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

            # DOCSTATUS SYNC (same as Sales Order)
            target_status = cint(ext.docstatus)
            frappe.clear_document_cache("Journal Entry", je.name)
            je = frappe.get_doc("Journal Entry", je.name)

            if target_status == 1:
                _safe_submit_doc(je, log_prefix=f"Journal Entry {je.name}")
            elif target_status == 2:
                _safe_submit_doc(je, log_prefix=f"Journal Entry {je.name}")
                je = frappe.get_doc("Journal Entry", je.name)
                _safe_cancel_doc(je, log_prefix=f"Journal Entry {je.name}")
            # target_status == 0 -> leave as Draft

            final_status = cint(frappe.db.get_value("Journal Entry", je.name, "docstatus"))
            result["created"].append({"name": je.name, "docstatus": final_status})
            
            frappe.db.set_value(
                "External Journal Entry",
                name,
                "is_synced",
                1,
                update_modified=False
            )


        except Exception as e:
            # FIX: roll back only this record's partial writes (draft
            # insert, GL Entries from a failed submit, rename SQL, etc.)
            # before logging. Earlier records in this same batch, already
            # committed at their own savepoints, are untouched.
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(frappe.get_traceback(), f"Sync Error: {name}")
            result["failed"].append({"external": name, "error": str(e)})
    
    return result