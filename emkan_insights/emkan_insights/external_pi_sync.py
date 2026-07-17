import frappe
import json
from frappe.utils import flt, cint

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "purchase_order_item",
    "purchase_receipt_item",
    "prevdoc_doctype",
    "prevdoc_docname"
}

# -----------------------------------------------------
# SAFE LOG ERROR (fixes 140-char title truncation)
# -----------------------------------------------------

def _log(title, message):
    """Log error with guaranteed short title."""
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


# -----------------------------------------------------
# FORCE CANCEL HELPERS
# -----------------------------------------------------

def _force_cancel_doc(doctype, docname):
    """
    Force cancel a document by directly updating the database.
    Use as fallback when normal cancel() fails due to linked docs or validations.
    """
    # Update main document
    frappe.db.sql("""
        UPDATE `tab{doctype}`
        SET docstatus = 2, 
            status = 'Cancelled', 
            modified = NOW(), 
            modified_by = SUBSTRING_INDEX(USER(), '@', 1)
        WHERE name = %s
    """.format(doctype=doctype), docname)
    
    # Update child tables
    meta = frappe.get_meta(doctype)
    for df in meta.get_table_fields():
        child_doctype = df.options
        if frappe.db.exists("DocType", child_doctype):
            frappe.db.sql("""
                UPDATE `tab{child_doctype}`
                SET docstatus = 2, 
                    modified = NOW(), 
                    modified_by = SUBSTRING_INDEX(USER(), '@', 1)
                WHERE parent = %s AND parenttype = %s
            """.format(child_doctype=child_doctype), (docname, doctype))
    
    # Clear document cache
    frappe.clear_document_cache(doctype, docname)


def _safe_cancel_doc(doc, log_prefix=""):
    """
    Safely cancel a document with fallback to force cancel.
    Returns True if successful, raises exception if both methods fail.
    """
    # First, try normal cancel
    try:
        doc.flags.ignore_permissions = True
        doc.flags.ignore_validate = True
        doc.flags.ignore_mandatory = True
        doc.flags.ignore_links = True
        doc.cancel()
        frappe.db.commit()
        _log(f"{log_prefix} cancelled normally", "")
        return True
    except Exception as normal_cancel_err:
        _log(f"{log_prefix} normal cancel failed, attempting force cancel",
             f"Error: {str(normal_cancel_err)}")
        
        # Fallback: Force cancel via SQL
        try:
            _force_cancel_doc(doc.doctype, doc.name)
            frappe.db.commit()
            _log(f"{log_prefix} force cancelled successfully", "")
            return True
        except Exception as force_cancel_err:
            _log(f"{log_prefix} force cancel also failed",
                 f"Normal: {str(normal_cancel_err)}\nForce: {str(force_cancel_err)}")
            raise


# -----------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------

def _build_company_prefixed_name(company_abbr, document_name):
    document_name = (document_name or "").strip()

    if not company_abbr or not document_name:
        return document_name

    if document_name.startswith(f"{company_abbr}-"):
        return document_name

    return f"{company_abbr}-{document_name}"


def resolve_local_po(remote_or_local_po):
    if not remote_or_local_po:
        return None

    local_name = frappe.db.get_value(
        "Purchase Order", {"remote_id": remote_or_local_po}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return remote_or_local_po

    # Try company-prefixed
    company_abbr = frappe.db.get_default("company_abbr") or ""
    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return prefixed

    return None


def resolve_local_pr(remote_or_local_pr, company_abbr=None):
    if not remote_or_local_pr:
        return None

    local_name = frappe.db.get_value(
        "Purchase Receipt", {"remote_id": remote_or_local_pr}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Receipt", remote_or_local_pr):
        return remote_or_local_pr

    if company_abbr and not remote_or_local_pr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pr}"
        if frappe.db.exists("Purchase Receipt", prefixed):
            return prefixed

    return None


def resolve_local_mr(remote_or_local_mr):
    if not remote_or_local_mr:
        return None

    local_name = frappe.db.get_value(
        "Material Request", {"remote_id": remote_or_local_mr}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Material Request", remote_or_local_mr):
        return remote_or_local_mr

    company_abbr = frappe.db.get_default("company_abbr") or ""
    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed

    return None


def get_po_detail(purchase_order, item_code):
    if not purchase_order:
        return None
    return frappe.db.get_value(
        "Purchase Order Item",
        {"parent": purchase_order, "item_code": item_code},
        "name"
    )


def get_pr_detail(purchase_receipt, item_code, remote_pr_item=None):
    if not purchase_receipt:
        return None

    if remote_pr_item:
        exact = frappe.db.get_value(
            "Purchase Receipt Item",
            {"parent": purchase_receipt, "custom_remote_id": remote_pr_item},
            "name"
        )
        if exact:
            return exact

    return frappe.db.get_value(
        "Purchase Receipt Item",
        {"parent": purchase_receipt, "item_code": item_code},
        "name"
    )


def get_mr_detail(material_request, item_code):
    if not material_request:
        return None
    return frappe.db.get_value(
        "Material Request Item",
        {"parent": material_request, "item_code": item_code},
        "name"
    )


# -----------------------------------------------------
# DUPLICATE CLEANER
# -----------------------------------------------------

def _dedupe_purchase_invoice_children(pi_name):
    """
    Remove duplicate child rows from Purchase Invoice.
    IMPORTANT: this must never raise — a dedupe failure should not make
    an otherwise-successful sync record look like it failed (see caller).
    """

    def _dedupe_table(doctype, parentfield, key_fields, float_fields=()):
        if not frappe.db.exists("DocType", doctype):
            return

        rows = frappe.db.get_all(
            doctype,
            filters={
                "parent": pi_name,
                "parenttype": "Purchase Invoice",
                "parentfield": parentfield,
            },
            fields=["name"] + key_fields + list(float_fields),
            order_by="creation asc",
        )

        seen = set()
        delete_rows = []

        for row in rows:
            key = []

            for field in key_fields:
                key.append(str(row.get(field) or "").strip().lower())

            for field in float_fields:
                key.append(round(flt(row.get(field)), 3))

            key = tuple(key)

            if key in seen:
                delete_rows.append(row.name)
            else:
                seen.add(key)

        if delete_rows:
            frappe.db.delete(
                doctype,
                {"name": ["in", delete_rows]}
            )

    # Purchase Invoice Item
    _dedupe_table(
        "Purchase Invoice Item",
        "items",
        [
            "item_code",
            "purchase_order",
            "purchase_receipt",
            "material_request",
            "warehouse",
            "uom",
        ],
        [
            "qty",
            "rate",
        ],
    )

    # Taxes
    _dedupe_table(
        "Purchase Taxes and Charges",
        "taxes",
        [
            "charge_type",
            "account_head",
            "description",
        ],
        [
            "rate",
            "tax_amount",
        ],
    )

    # Payment Schedule
    _dedupe_table(
        "Payment Schedule",
        "payment_schedule",
        [
            "payment_term",
            "due_date",
        ],
        [
            "payment_amount",
            "invoice_portion",
        ],
    )

    frappe.db.commit()


def _safe_dedupe(pi_name):
    """
    Wrapper so a bug inside dedupe (or any future one) can never bubble up
    and get caught by the outer per-record try/except, which would mark an
    already-inserted, already-committed invoice as "failed" while leaving
    duplicate rows in place and skipping it forever afterwards (because the
    'existing' check would find remote_id already set on retry).
    """
    try:
        _dedupe_purchase_invoice_children(pi_name)
    except Exception:
        _log(
            title=f"PI dedupe failed for {pi_name}",
            message=frappe.get_traceback()
        )


def _delete_purchase_invoice_child_rows(pi_name):
    meta = frappe.get_meta("Purchase Invoice")

    for df in meta.get_table_fields():
        if frappe.db.exists("DocType", df.options):
            frappe.db.delete(
                df.options,
                {
                    "parent": pi_name,
                    "parenttype": "Purchase Invoice",
                    "parentfield": df.fieldname,
                },
            )


def _get_child_count(doctype, parent, parenttype):
    return frappe.db.count(doctype, {"parent": parent, "parenttype": parenttype})


def _has_item_count_mismatch(ext_pi, pi_name):
    external_items = _get_child_count("Purchase Invoice Item", ext_pi.name, ext_pi.doctype)
    local_items = _get_child_count("Purchase Invoice Item", pi_name, "Purchase Invoice")

    return external_items != local_items, external_items, local_items


def _get_table_fieldnames(meta):
    return {df.fieldname for df in meta.get_table_fields()}


def _copy_header_fields(source_doc, target_doc):
    """
    Copy scalar fields only. Child tables must be rebuilt deliberately so
    remote child row names cannot collide with local Purchase Invoice rows.
    """
    target_table_fields = _get_table_fieldnames(target_doc.meta)

    for field, value in source_doc.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field == "remote_id":
            continue
        if field in target_table_fields:
            continue
        if hasattr(target_doc, field):
            target_doc.set(field, value)


def _copy_child_row_fields(row, target_child_doctype):
    target_meta = frappe.get_meta(target_child_doctype)
    target_table_fields = _get_table_fieldnames(target_meta)
    row_data = {"doctype": target_child_doctype}

    for field, value in row.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field in target_table_fields:
            continue
        if target_meta.has_field(field):
            row_data[field] = value

    return row_data


# -----------------------------------------------------
# CRITICAL FIX: Insert items one-by-one with error isolation
# -----------------------------------------------------

def _insert_pi_items_safely(pi_name, ext_pi, company_abbr=None):
    """
    Insert Purchase Invoice Items one at a time, catching each error
    so one bad item doesn't kill the entire batch.
    Returns: (success_count, error_list)
    """
    if not ext_pi.items:
        return 0, []

    success_count = 0
    errors = []

    for idx, row in enumerate(ext_pi.items, start=1):
        try:
            item_row = {"doctype": "Purchase Invoice Item"}

            # Copy fields safely
            for field, value in row.as_dict().items():
                if field in SYSTEM_FIELDS:
                    continue
                if field in IGNORE_ITEM_FIELDS:
                    continue
                # Skip link fields we'll resolve manually
                if field in {"purchase_order", "purchase_receipt",
                             "po_detail", "pr_detail", "material_request",
                             "material_request_item"}:
                    continue
                item_row[field] = value

            # Resolve PO
            if row.purchase_order:
                local_po = resolve_local_po(row.purchase_order)
                item_row["purchase_order"] = local_po
                item_row["po_detail"] = get_po_detail(local_po, row.item_code) if local_po else None
            else:
                item_row["purchase_order"] = None
                item_row["po_detail"] = None

            # Resolve PR
            if row.purchase_receipt:
                local_pr = resolve_local_pr(row.purchase_receipt, company_abbr)
                item_row["purchase_receipt"] = local_pr
                item_row["pr_detail"] = get_pr_detail(
                    local_pr,
                    row.item_code,
                    remote_pr_item=row.get("purchase_receipt_item")
                ) if local_pr else None
            else:
                item_row["purchase_receipt"] = None
                item_row["pr_detail"] = None

            # Resolve MR
            if row.get("material_request"):
                local_mr = resolve_local_mr(row.material_request)
                item_row["material_request"] = local_mr
                item_row["material_request_item"] = get_mr_detail(local_mr, row.item_code) if local_mr else None
            else:
                item_row["material_request"] = None
                item_row["material_request_item"] = None

            # Parent linkage
            item_row["parent"] = pi_name
            item_row["parentfield"] = "items"
            item_row["parenttype"] = "Purchase Invoice"
            item_row["idx"] = idx

            # Generate unique name to avoid any collision
            item_row["name"] = frappe.generate_hash(length=10)

            # CRITICAL: Use db_insert() directly to bypass ALL validation
            # that might fail on individual items
            child_doc = frappe.get_doc(item_row)
            child_doc.db_insert()

            success_count += 1

        except Exception as e:
            error_msg = f"Item {idx} (row name: {row.name}, item_code: {row.item_code}): {str(e)}"
            errors.append(error_msg)
            _log(
                title=f"PI item insert failed for {pi_name}",
                message=error_msg
            )

    if success_count > 0:
        frappe.db.commit()

    return success_count, errors


# -----------------------------------------------------
# CRITICAL FIX: Handle existing doc docstatus sync
# -----------------------------------------------------

def _sync_existing_docstatus(existing_name, ext_pi):
    """
    Handle docstatus sync for already-existing Purchase Invoice documents.
    This is the KEY FIX - previously existing docs were just skipped.
    """
    local_docstatus = frappe.db.get_value("Purchase Invoice", existing_name, "docstatus")
    target_docstatus = cint(ext_pi.docstatus)

    # Already in sync
    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists"}

    try:
        pi = frappe.get_doc("Purchase Invoice", existing_name)
        pi.flags.ignore_permissions = True
        pi.flags.ignore_validate = True
        pi.flags.ignore_mandatory = True
        pi.flags.ignore_links = True

        if target_docstatus == 1:
            # External is submitted - we need to submit local too
            if local_docstatus == 0:
                # Draft -> Submitted
                pi.submit()
                frappe.db.commit()
                _safe_dedupe(pi.name)
                frappe.db.commit()
                _log(f"PI {pi.name} docstatus synced: Draft -> Submitted", "")
                return {"name": pi.name, "status": "docstatus_synced", "docstatus": 1}
            else:
                # Cancelled -> Submitted (cannot un-cancel in Frappe)
                _log(
                    f"PI {existing_name} cannot transition from Cancelled to Submitted",
                    f"local={local_docstatus}, external={target_docstatus}"
                )
                return {"name": existing_name, "status": "exists", "warning": "cannot un-cancel"}

        elif target_docstatus == 2:
            # External is cancelled - we need to cancel local too
            if local_docstatus == 0:
                # Draft -> need to submit first, then cancel
                pi.submit()
                frappe.db.commit()
                pi = frappe.get_doc("Purchase Invoice", existing_name)
            
            # Now cancel (whether it was 0 or 1 before)
            _safe_cancel_doc(pi, log_prefix=f"PI {existing_name}")
            _safe_dedupe(pi.name)
            frappe.db.commit()
            
            # Verify the cancel worked
            final_docstatus = frappe.db.get_value("Purchase Invoice", pi.name, "docstatus")
            if final_docstatus == 2:
                _log(f"PI {pi.name} docstatus synced: -> Cancelled", "")
                return {"name": pi.name, "status": "docstatus_synced", "docstatus": 2}
            else:
                _log(f"PI {pi.name} cancel verification failed",
                     f"Expected docstatus=2, got {final_docstatus}")
                return {"name": pi.name, "status": "exists", "warning": "cancel verification failed"}

        else:
            # External is Draft (0) but local is Submitted (1) or Cancelled (2)
            # In Frappe, you can't un-submit or un-cancel
            _log(
                f"PI {existing_name} cannot transition to Draft",
                f"local={local_docstatus}, external={target_docstatus}"
            )
            return {"name": existing_name, "status": "exists", "warning": "cannot revert to draft"}

    except Exception as trans_err:
        _log(f"PI {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(trans_err)}


# -----------------------------------------------------
# MAIN SYNC
# -----------------------------------------------------

@frappe.whitelist()
def sync_external_purchase_invoice_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:

        try:

            # Force fresh transaction snapshot
            frappe.db.commit()

            ext_pi = frappe.get_doc(source_doctype, name)
            remote_id = ext_pi.name

            # Defensive reload if items empty
            if not ext_pi.items:
                frappe.clear_document_cache(source_doctype, name)
                ext_pi.reload()

                if not ext_pi.items:
                    actual_count = frappe.db.count(f"{source_doctype} Item", {"parent": name})
                    if actual_count > 0:
                        ext_pi = frappe.get_doc(source_doctype, name)
                        for field in ext_pi.meta.get_table_fields():
                            ext_pi.load_children(field.fieldname)

                    if not ext_pi.items:
                        _log(
                            title="PI Sync - external doc has 0 items",
                            message=f"External PI {name} has 0 items even after reload. "
                                    f"Proceeding with empty items table."
                        )

            # Build target name
            company_abbr = frappe.db.get_value('Company', ext_pi.company, 'abbr') or ''

            if not company_abbr:
                _log(
                    title="PI Sync - missing company abbr",
                    message=f"No company abbr for '{ext_pi.company}' on {remote_id}."
                )

            target_name = _build_company_prefixed_name(company_abbr, remote_id)

            # Check already synced
            existing = frappe.db.get_value(
                "Purchase Invoice", {"remote_id": remote_id}, "name"
            )

            if not existing:
                existing = frappe.db.exists("Purchase Invoice", target_name)

            # ============================================================
            # KEY FIX: Sync docstatus for existing documents instead of skip
            # ============================================================
            if existing:
                local_docstatus = cint(frappe.db.get_value("Purchase Invoice", existing, "docstatus"))
                target_docstatus = cint(ext_pi.docstatus)
                has_item_mismatch, expected_items, actual_items = _has_item_count_mismatch(ext_pi, existing)

                if local_docstatus == 0 and (target_docstatus in (1, 2) or has_item_mismatch):
                    _log(
                        f"PI {existing} rebuilding draft before docstatus sync",
                        f"External docstatus is {target_docstatus}; deleting local draft and rebuilding from {remote_id}. "
                        f"External items: {expected_items}, local items: {actual_items}."
                    )
                    _delete_purchase_invoice_child_rows(existing)
                    frappe.delete_doc(
                        "Purchase Invoice",
                        existing,
                        force=True,
                        ignore_permissions=True,
                        ignore_missing=True,
                    )
                    _delete_purchase_invoice_child_rows(existing)
                    frappe.db.commit()
                elif has_item_mismatch:
                    _log(
                        f"PI {existing} item count mismatch",
                        f"Cannot rebuild non-draft Purchase Invoice safely. "
                        f"External docstatus: {target_docstatus}, local docstatus: {local_docstatus}. "
                        f"External items: {expected_items}, local items: {actual_items}."
                    )
                    results.append({
                        "name": existing,
                        "status": "sync_error",
                        "docstatus": local_docstatus,
                        "expected_docstatus": target_docstatus,
                        "items_synced": actual_items,
                        "items_expected": expected_items,
                        "error": "Item count mismatch on non-draft Purchase Invoice; manual rebuild needed."
                    })
                    continue
                else:
                    results.append(_sync_existing_docstatus(existing, ext_pi))
                    continue

            _delete_purchase_invoice_child_rows(target_name)
            frappe.db.commit()

            pi = frappe.new_doc("Purchase Invoice")

            # Set name BEFORE anything else
            pi.remote_id = remote_id
            pi.name = target_name
            pi.flags.name_set = True

            if hasattr(pi, "source_site"):
                pi.source_site = ext_pi.source_site

            # Copy scalar header fields only. Child tables are rebuilt below.
            _copy_header_fields(ext_pi, pi)

            # Fix currency
            if not pi.currency:
                pi.currency = frappe.get_cached_value(
                    "Company", pi.company, "default_currency"
                )

            # Handle return
            if ext_pi.get("is_return"):
                if frappe.db.exists("Purchase Invoice", ext_pi.return_against):
                    pi.is_return = 1
                    pi.return_against = ext_pi.return_against
                else:
                    pi.is_return = 0
                    pi.return_against = None

            # -------------------------------------------------
            # ITEMS: Use safe one-by-one insert instead of append+insert
            # -------------------------------------------------

            # Clear items on parent (won't be saved via normal insert)
            pi.set("items", [])

            # -------------------------------------------------
            # TAXES
            # -------------------------------------------------

            if ext_pi.get("taxes"):
                pi.set("taxes", [])
                for row in ext_pi.taxes:
                    tax_row = _copy_child_row_fields(row, "Purchase Taxes and Charges")
                    pi.append("taxes", tax_row)

            # -------------------------------------------------
            # PAYMENT SCHEDULE (rebuild with fresh names)
            # -------------------------------------------------

            if ext_pi.get("payment_schedule"):
                pi.set("payment_schedule", [])
                for idx, row in enumerate(ext_pi.payment_schedule, start=1):
                    pay_row = {
                        "doctype": "Payment Schedule",
                        "idx": idx,
                        "payment_term": row.payment_term,
                        "description": row.description,
                        "due_date": row.due_date,
                        "invoice_portion": row.invoice_portion,
                        "discount": row.discount,
                        "payment_amount": row.payment_amount,
                        "base_payment_amount": row.base_payment_amount,
                    }
                    pi.append("payment_schedule", pay_row)

            # -------------------------------------------------
            # FLAGS
            # -------------------------------------------------

            pi.flags.ignore_permissions = True
            pi.flags.ignore_validate = True
            pi.flags.ignore_mandatory = True
            pi.flags.ignore_naming_series = True

            # -------------------------------------------------
            # INSERT PARENT ONLY (no items yet)
            # -------------------------------------------------

            pi.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            frappe.db.commit()

            # Ensure remote_id is set (safety net)
            frappe.db.set_value("Purchase Invoice", pi.name, "remote_id", remote_id, update_modified=False)
            frappe.db.commit()

            # -------------------------------------------------
            # NOW INSERT ITEMS ONE-BY-ONE WITH ERROR ISOLATION
            # -------------------------------------------------

            success_count, item_errors = _insert_pi_items_safely(pi.name, ext_pi, company_abbr)

            if item_errors:
                _log(
                    title=f"PI {pi.name} - {len(item_errors)} item errors",
                    message="Item insert errors:\n" + "\n".join(item_errors)
                )

            # -------------------------------------------------
            # VERIFY
            # -------------------------------------------------

            actual_item_count = _get_child_count(
                "Purchase Invoice Item", pi.name, "Purchase Invoice"
            )
            expected_item_count = len(ext_pi.get("items") or [])

            if actual_item_count != expected_item_count:
                _log(
                    title=f"PI Sync - items mismatch for {pi.name}",
                    message=f"Expected {expected_item_count} items, "
                            f"found {actual_item_count} after insert. "
                            f"Successfully inserted: {success_count}, "
                            f"Errors: {len(item_errors)}."
                )

            # Clean duplicates
            _safe_dedupe(pi.name)

            # -------------------------------------------------
            # NAMING SAFETY NET
            # -------------------------------------------------

            if pi.name != target_name:

                if not frappe.db.exists("Purchase Invoice", target_name):

                    old_name = pi.name

                    frappe.db.sql("""
                        UPDATE `tabPurchase Invoice`
                        SET name=%s
                        WHERE name=%s
                    """, (target_name, old_name))

                    for child in [
                        "Purchase Invoice Item",
                        "Purchase Taxes and Charges",
                        "Payment Schedule",
                        "Purchase Invoice Advance",
                        "Purchase Invoice Deduction",
                        "Purchase Invoice Tax",
                    ]:
                        if frappe.db.exists("DocType", child):
                            frappe.db.sql(f"""
                                UPDATE `tab{child}`
                                SET parent=%s
                                WHERE parent=%s
                            """, (target_name, old_name))

                    frappe.db.commit()
                    pi.name = target_name

                else:
                    _log(
                        title="PI Sync - name collision",
                        message=f"Cannot rename {pi.name} to {target_name} - already exists."
                    )

            frappe.clear_document_cache("Purchase Invoice", pi.name)
            pi = frappe.get_doc("Purchase Invoice", pi.name)

            pi.flags.ignore_permissions = True
            pi.flags.ignore_validate = True
            pi.flags.ignore_mandatory = True
            pi.flags.ignore_naming_series = True
            pi.flags.ignore_links = True

            # -------------------------------------------------
            # DOCSTATUS SYNC FOR NEW DOCUMENTS
            # -------------------------------------------------

            target_docstatus = cint(ext_pi.docstatus)

            _log(f"PI {pi.name} pre-docstatus-sync",
                 f"External docstatus: {target_docstatus}, Current docstatus: {pi.docstatus}")

            if target_docstatus == 1:
                # Submit
                try:
                    pi.submit()
                    frappe.db.commit()
                    _safe_dedupe(pi.name)
                    frappe.db.commit()
                    _log(f"PI {pi.name} submitted successfully", "")
                    
                    actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                    results.append({
                        "name": pi.name,
                        "status": "synced",
                        "docstatus": 1,
                        "items_synced": actual_item_count,
                        "items_expected": expected_item_count
                    })

                except Exception as submit_err:
                    tb = frappe.get_traceback()
                    _log(f"PI {pi.name} SUBMIT FAILED",
                         f"Error: {str(submit_err)}\n\nTraceback:\n{tb}")
                    
                    actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                    results.append({
                        "name": pi.name,
                        "status": "sync_error",
                        "docstatus": frappe.db.get_value("Purchase Invoice", pi.name, "docstatus"),
                        "expected_docstatus": 1,
                        "error": f"Submit failed: {str(submit_err)}",
                        "items_synced": actual_item_count,
                        "items_expected": expected_item_count
                    })

            elif target_docstatus == 2:
                # Submit first, then cancel
                try:
                    pi.submit()
                    frappe.db.commit()
                    _safe_dedupe(pi.name)
                    frappe.db.commit()
                    _log(f"PI {pi.name} submitted (step 1 of cancel)", "")

                    # Re-fetch and cancel
                    pi = frappe.get_doc("Purchase Invoice", pi.name)
                    _safe_cancel_doc(pi, log_prefix=f"PI {pi.name}")
                    
                    _safe_dedupe(pi.name)
                    frappe.db.commit()
                    
                    # Verify
                    final_docstatus = frappe.db.get_value("Purchase Invoice", pi.name, "docstatus")
                    final_status = frappe.db.get_value("Purchase Invoice", pi.name, "status")
                    
                    _log(f"PI {pi.name} cancel complete",
                         f"Final docstatus: {final_docstatus}, Final status: {final_status}")
                    
                    actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                    
                    if final_docstatus == 2:
                        results.append({
                            "name": pi.name,
                            "status": "synced",
                            "docstatus": 2,
                            "items_synced": actual_item_count,
                            "items_expected": expected_item_count
                        })
                    else:
                        _log(f"PI {pi.name} CANCEL VERIFICATION FAILED",
                             f"Expected docstatus=2, got {final_docstatus}")
                        results.append({
                            "name": pi.name,
                            "status": "sync_error",
                            "error": f"Cancel verification failed - docstatus is {final_docstatus}",
                            "items_synced": actual_item_count,
                            "items_expected": expected_item_count
                        })

                except Exception as cancel_err:
                    tb = frappe.get_traceback()
                    _log(f"PI {pi.name} CANCEL FAILED - CRITICAL",
                         f"Error: {str(cancel_err)}\n\nTraceback:\n{tb}")
                    
                    # Last resort: Force cancel
                    try:
                        _force_cancel_doc("Purchase Invoice", pi.name)
                        frappe.db.commit()
                        _log(f"PI {pi.name} force cancelled as last resort", "")
                        
                        actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                        results.append({
                            "name": pi.name,
                            "status": "synced",
                            "docstatus": 2,
                            "items_synced": actual_item_count,
                            "items_expected": expected_item_count
                        })
                    except Exception as force_err:
                        _log(f"PI {pi.name} FORCE CANCEL ALSO FAILED",
                             f"Force error: {str(force_err)}")
                        
                        actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                        results.append({
                            "name": pi.name,
                            "status": "synced_submitted",
                            "error": f"Cancel failed: {str(cancel_err)}",
                            "items_synced": actual_item_count,
                            "items_expected": expected_item_count
                        })

            else:
                # docstatus = 0 (Draft) - already in correct state
                actual_item_count = _get_child_count("Purchase Invoice Item", pi.name, "Purchase Invoice")
                results.append({
                    "name": pi.name,
                    "status": "synced",
                    "docstatus": 0,
                    "items_synced": actual_item_count,
                    "items_expected": expected_item_count
                })

            frappe.db.commit()

        except Exception as e:

            error = frappe.get_traceback()

            _log(
                title=f"Purchase Invoice Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results
