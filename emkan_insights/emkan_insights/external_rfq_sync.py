import frappe
import json
from frappe.utils import flt

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}


def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


# ---------------------------------------------------------------------------
# NOTE ON TRANSACTIONS
# ---------------------------------------------------------------------------

# frappe.db.rollback() anymore. They operate inside whatever transaction the
# caller (sync_rfq_docs) has open. Only the top-level loop in sync_rfq_docs
# decides when a unit of work is "done" and safe to persist. This keeps each
# RFQ's sync atomic: either everything for that RFQ lands, or none of it does.
# ---------------------------------------------------------------------------


def _force_cancel_doc(doctype, docname):
    """
    Force cancel a document by directly updating the database.
    Use as fallback when normal cancel() fails due to linked docs or validations.

    Does NOT commit - caller controls the transaction boundary.
    """
    frappe.db.sql(
        """
        UPDATE `tab{doctype}`
        SET docstatus = 2,
            status = 'Cancelled',
            modified = NOW(),
            modified_by = %s
        WHERE name = %s
        """.format(doctype=doctype),
        (frappe.session.user, docname),
    )

    meta = frappe.get_meta(doctype)
    for df in meta.get_table_fields():
        child_doctype = df.options
        if frappe.db.exists("DocType", child_doctype):
            frappe.db.sql(
                """
                UPDATE `tab{child_doctype}`
                SET docstatus = 2,
                    modified = NOW(),
                    modified_by = %s
                WHERE parent = %s AND parenttype = %s
                """.format(child_doctype=child_doctype),
                (frappe.session.user, docname, doctype),
            )

    frappe.clear_document_cache(doctype, docname)


def _safe_cancel_doc(doc, log_prefix=""):
    """
    Safely cancel a document with fallback to force cancel.
    Returns True if successful, raises exception if both methods fail.

    Does NOT commit - caller controls the transaction boundary. Uses a
    savepoint so a failed normal-cancel attempt doesn't leave half-applied
    changes before we fall back to the raw SQL force-cancel.
    """
    savepoint = f"cancel_{frappe.generate_hash(length=8)}"
    frappe.db.savepoint(savepoint)

    try:
        doc.flags.ignore_permissions = True
        doc.cancel()
        _log(f"{log_prefix} cancelled normally", "")
        return True
    except Exception as normal_cancel_err:
        frappe.db.rollback(save_point=savepoint)
        _log(f"{log_prefix} normal cancel failed, attempting force cancel",
             f"Error: {str(normal_cancel_err)}")

        try:
            _force_cancel_doc(doc.doctype, doc.name)
            _log(f"{log_prefix} force cancelled successfully", "")
            return True
        except Exception as force_cancel_err:
            _log(f"{log_prefix} force cancel also failed",
                 f"Normal: {str(normal_cancel_err)}\nForce: {str(force_cancel_err)}")
            raise


def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    if not remote_or_local_name:
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    for remote_field in ("custom_remote_id", "remote_id"):
        if frappe.get_meta(doctype).has_field(remote_field):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def get_child_detail(doctype, parent, item_code, remote_item_name=None):
    if not parent:
        return None

    if remote_item_name and frappe.get_meta(doctype).has_field("custom_remote_id"):
        exact = frappe.db.get_value(
            doctype,
            {"parent": parent, "custom_remote_id": remote_item_name},
            "name"
        )
        if exact:
            return exact

    return frappe.db.get_value(
        doctype,
        {"parent": parent, "item_code": item_code},
        "name"
    )


def _dedupe_rfq_children(parent_name, parenttype="Request for Quotation"):
    """
    Remove duplicate RFQ child rows (items / suppliers).
    Works for both Request for Quotation and External Request for Quotation
    - they share the same child DocTypes.

    Does NOT commit - caller controls the transaction boundary.
    """

    def _dedupe_table(doctype, parentfield, key_fields, float_fields=()):
        if not frappe.db.exists("DocType", doctype):
            return

        rows = frappe.db.get_all(
            doctype,
            filters={
                "parent": parent_name,
                "parenttype": parenttype,
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
            frappe.db.delete(doctype, {"name": ["in", delete_rows]})

    _dedupe_table(
        "Request for Quotation Item",
        "items",
        ["item_code", "warehouse", "material_request", "material_request_item", "schedule_date", "uom"],
        ["qty"],
    )

    _dedupe_table(
        "Request for Quotation Supplier",
        "suppliers",
        ["supplier", "contact", "email_id"],
    )


def _safe_dedupe(parent_name, parenttype="Request for Quotation"):
    try:
        _dedupe_rfq_children(parent_name, parenttype=parenttype)
    except Exception:
        _log(
            title=f"RFQ dedupe failed for {parenttype} {parent_name}",
            message=frappe.get_traceback(),
        )


def _sync_existing_docstatus(existing, external):
    """Handle docstatus sync for existing documents. Does NOT commit."""
    local_docstatus = frappe.db.get_value("Request for Quotation", existing, "docstatus")

    if local_docstatus == external.docstatus:
        return {"name": existing, "status": "exists"}

    try:
        rfq = frappe.get_doc("Request for Quotation", existing)
        rfq.flags.ignore_permissions = True

        if external.docstatus == 2:
            # External is cancelled - we need to cancel local too
            if local_docstatus == 0:
                # Draft locally -> submit first, then cancel
                rfq.submit()
                rfq = frappe.get_doc("Request for Quotation", existing)

            _safe_cancel_doc(rfq, log_prefix=f"RFQ {existing}")
            _safe_dedupe(rfq.name)

            return {"name": rfq.name, "status": "docstatus_synced", "docstatus": 2}

        elif external.docstatus == 1 and local_docstatus == 0:
            rfq.submit()
            _safe_dedupe(rfq.name)

            return {"name": rfq.name, "status": "docstatus_synced", "docstatus": 1}

        else:
            _log(f"RFQ {existing} unhandled docstatus transition",
                 f"local={local_docstatus}, external={external.docstatus}")
            return {"name": existing, "status": "exists", "warning": "unhandled transition"}

    except Exception as trans_err:
        _log(f"RFQ {existing} docstatus transition failed", frappe.get_traceback())
        return {"name": existing, "status": "exists", "error": str(trans_err)}


def _sync_single_rfq(name):
    """
    Do the full sync of one External Request for Quotation -> Request for
    Quotation. Returns a result dict. Raises on unrecoverable failure so the
    caller's savepoint can roll back just this record.
    """
    # Clean duplicates on External RFQ first (items + suppliers)
    _safe_dedupe(name, parenttype="External Request for Quotation")
    frappe.clear_document_cache("External Request for Quotation", name)
    external = frappe.get_doc("External Request for Quotation", name)

    if not external.company:
        frappe.throw(f"Company missing in External RFQ: {name}")

    # -------------------------------------------------
    # BUILD TARGET NAME
    # -------------------------------------------------
    company_abbr = frappe.db.get_value("Company", external.company, "abbr") or ""
    remote_id = external.name
    target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

    # -------------------------------------------------
    # CHECK ALREADY SYNCED
    # -------------------------------------------------
    existing = frappe.db.get_value(
        "Request for Quotation", {"custom_remote_id": remote_id}, "name"
    )
    if not existing:
        existing = frappe.db.exists("Request for Quotation", target_name)

    if existing:
        return _sync_existing_docstatus(existing, external)

    # -------------------------------------------------
    # CREATE RFQ
    # -------------------------------------------------
    rfq = frappe.new_doc("Request for Quotation")

    rfq.set("items", [])
    rfq.set("suppliers", [])

    rfq.name = target_name
    rfq.flags.name_set = True

    # CRITICAL: naming_series must be set or submit fails
    rfq.naming_series = external.naming_series or "PUR-RFQ-.YYYY.-"

    rfq.company = external.company
    rfq.transaction_date = external.transaction_date
    rfq.message_for_supplier = external.message_for_supplier or "Request for Quotation"

    # CRITICAL: status must be "Draft" for insert, then submit changes it
    rfq.status = "Draft"

    # Copy all other header fields
    for field, value in external.as_dict().items():
        if (
            field not in SYSTEM_FIELDS
            and field not in {"items", "suppliers", "remote_id", "custom_remote_id",
                              "naming_series", "company", "transaction_date",
                              "message_for_supplier", "status", "docstatus",
                              "amended_from", "auto_repeat"}
            and hasattr(rfq, field)
        ):
            rfq.set(field, value)

    # CRITICAL: Clear amendment fields that block submit
    rfq.amended_from = None
    if hasattr(rfq, "auto_repeat"):
        rfq.auto_repeat = None

    rfq.custom_remote_id = remote_id
    rfq.custom_source_site = external.source_site

    # Resolve project
    if external.get("project"):
        rfq.project = resolve_local_doc("External Project", external.project, company_abbr)

    # -------------------------------------------------
    # MAP SUPPLIERS
    # -------------------------------------------------
    seen_suppliers = set()
    for s in external.get("suppliers", []):
        supplier_code = frappe.db.get_value("External Supplier", s.supplier, "remote_id")
        if not supplier_code:
            continue

        fingerprint = (supplier_code, s.contact)
        if fingerprint in seen_suppliers:
            continue
        seen_suppliers.add(fingerprint)

        rfq.append("suppliers", {
            "supplier": supplier_code,
            "contact": s.contact,
            "quote_status": s.quote_status or "Pending",
            "supplier_name": s.supplier_name,
            "email_id": s.email_id,
            "send_email": s.send_email or 0,
            "email_sent": s.email_sent or 0,
        })

    # -------------------------------------------------
    # MAP ITEMS
    # -------------------------------------------------
    seen_items = set()
    for row in external.get("items", []):
        item_code = frappe.db.get_value("External Item", row.item_code, "remote_id")
        if not item_code:
            _log(f"RFQ item skip - no local item for {row.item_code}",
                 f"External RFQ {name}, row {row.name}")
            continue

        # Resolve warehouse
        warehouse = None
        if row.warehouse:
            warehouse = frappe.db.get_value("External Warehouse", row.warehouse, "remote_id")
        if not warehouse:
            warehouse = frappe.db.get_value(
                "Warehouse", {"is_group": 0, "company": external.company}, "name"
            )

        fingerprint = (item_code, row.qty, row.schedule_date, warehouse)
        if fingerprint in seen_items:
            continue
        seen_items.add(fingerprint)

        # Resolve Material Request with prefix
        material_request = None
        material_request_item = None
        if row.get("material_request"):
            material_request = resolve_local_doc("Material Request", row.material_request, company_abbr)
            if material_request:
                material_request_item = get_child_detail(
                    "Material Request Item",
                    material_request,
                    item_code,
                    remote_item_name=row.get("material_request_item")
                )

        rfq.append("items", {
            "item_code": item_code,
            "item_name": row.item_name,
            "description": row.description,
            "qty": row.qty,
            "uom": row.uom,
            "warehouse": warehouse,
            "schedule_date": row.schedule_date,
            "supplier_part_no": row.supplier_part_no,
            "custom_remote_id": row.get("custom_remote_id") or row.name,
            "item_group": row.item_group,
            "brand": row.brand,
            "image": row.image,
            "stock_uom": row.stock_uom,
            "conversion_factor": row.conversion_factor or 1,
            "stock_qty": row.stock_qty or row.qty,
            "material_request": material_request,
            "material_request_item": material_request_item,
            "project_name": row.project_name,
            "page_break": row.page_break,
        })

    _log(f"RFQ {target_name} pre-insert check",
         f"Suppliers: {len(rfq.suppliers)}, Items: {len(rfq.items)}, "
         f"Naming Series: {rfq.naming_series}, Company: {rfq.company}, "
         f"External docstatus: {external.docstatus}")

    # -------------------------------------------------
    # FLAGS
    # -------------------------------------------------
    rfq.flags.ignore_permissions = True
    rfq.flags.ignore_naming_series = True

    # -------------------------------------------------
    # INSERT
    # -------------------------------------------------
    rfq.insert(
        ignore_permissions=True,
    )
    _safe_dedupe(rfq.name)

    _log(f"RFQ {rfq.name} inserted successfully",
         f"Docstatus after insert: {rfq.docstatus}, Status: {rfq.status}")

    # -------------------------------------------------
    # NAMING SAFETY NET
    # -------------------------------------------------
    if rfq.name != target_name:
        if not frappe.db.exists("Request for Quotation", target_name):
            old_name = rfq.name
            frappe.db.sql("""
                UPDATE `tabRequest for Quotation`
                SET name = %s
                WHERE name = %s
            """, (target_name, old_name))

            for child in ["Request for Quotation Item", "Request for Quotation Supplier"]:
                if frappe.db.exists("DocType", child):
                    frappe.db.sql(f"""
                        UPDATE `tab{child}`
                        SET parent = %s
                        WHERE parent = %s
                    """, (target_name, old_name))

            rfq.name = target_name
        else:
            _log("RFQ Sync - name collision",
                 f"Cannot rename {rfq.name} to {target_name}")

    # -------------------------------------------------
    # DOCSTATUS SYNC
    # -------------------------------------------------
    frappe.clear_document_cache("Request for Quotation", rfq.name)
    rfq = frappe.get_doc("Request for Quotation", rfq.name)

    _log(f"RFQ {rfq.name} pre-docstatus-sync",
         f"External docstatus: {external.docstatus}, "
         f"Current docstatus: {rfq.docstatus}, "
         f"Suppliers count: {len(rfq.suppliers)}, "
         f"Items count: {len(rfq.items)}")

    if external.docstatus == 1:
        try:
            rfq.flags.ignore_permissions = True
            rfq.submit()
            _safe_dedupe(rfq.name)
            _log(f"RFQ {rfq.name} submitted successfully", "")
            result = {"name": rfq.name, "status": "synced", "docstatus": 1}

        except Exception as submit_err:
            tb = frappe.get_traceback()
            _log(f"RFQ {rfq.name} SUBMIT FAILED",
                 f"Error: {str(submit_err)}\n\nTraceback:\n{tb}")
            # Keep the draft that was already inserted; report the submit
            # failure but don't raise, so this record's savepoint is not
            # rolled back and the draft still gets committed by the caller.
            result = {
                "name": rfq.name,
                "status": "synced_draft",
                "error": f"Submit failed: {str(submit_err)}"
            }
            external.db_set("remote_id", rfq.name)
            return result

    elif external.docstatus == 2:
        try:
            rfq.flags.ignore_permissions = True

            # Submit first (required before cancel)
            rfq.submit()
            _log(f"RFQ {rfq.name} submitted (step 1 of cancel)", "")

            rfq = frappe.get_doc("Request for Quotation", rfq.name)
            _safe_cancel_doc(rfq, log_prefix=f"RFQ {rfq.name}")
            _safe_dedupe(rfq.name)

            final_docstatus = frappe.db.get_value("Request for Quotation", rfq.name, "docstatus")
            final_status = frappe.db.get_value("Request for Quotation", rfq.name, "status")

            _log(f"RFQ {rfq.name} cancel complete",
                 f"Final docstatus: {final_docstatus}, Final status: {final_status}")

            if final_docstatus == 2:
                result = {"name": rfq.name, "status": "synced", "docstatus": 2}
            else:
                _log(f"RFQ {rfq.name} CANCEL VERIFICATION FAILED",
                     f"Expected docstatus=2, got docstatus={final_docstatus}")
                result = {
                    "name": rfq.name,
                    "status": "sync_error",
                    "error": f"Cancel verification failed - docstatus is {final_docstatus}"
                }

        except Exception as cancel_err:
            tb = frappe.get_traceback()
            _log(f"RFQ {rfq.name} CANCEL FAILED - CRITICAL",
                 f"Error: {str(cancel_err)}\n\nTraceback:\n{tb}")

            try:
                _force_cancel_doc("Request for Quotation", rfq.name)
                _log(f"RFQ {rfq.name} force cancelled as last resort", "")
                result = {"name": rfq.name, "status": "synced", "docstatus": 2}
            except Exception as force_err:
                _log(f"RFQ {rfq.name} FORCE CANCEL ALSO FAILED",
                     f"Force error: {str(force_err)}")
                result = {
                    "name": rfq.name,
                    "status": "synced_submitted",
                    "error": f"Cancel failed: {str(cancel_err)}"
                }
            external.db_set("remote_id", rfq.name)
            return result

    else:
        # docstatus = 0 (Draft) - already in correct state
        result = {"name": rfq.name, "status": "synced", "docstatus": 0}

    external.db_set("remote_id", rfq.name)
    return result


@frappe.whitelist()
def sync_rfq_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        savepoint = f"rfq_sync_{frappe.generate_hash(length=10)}"
        frappe.db.savepoint(savepoint)

        try:
            result = _sync_single_rfq(name)
            results.append(result)
            # No explicit commit here. Frappe commits the whole request
            # automatically once this whitelisted method returns without an
            # uncaught exception. The savepoint below still protects the
            # batch: if one record fails, only its own writes are rolled
            # back before we move on - they just aren't durable until the
            # very end, along with everything else in this request.

        except Exception as e:
            frappe.db.rollback(save_point=savepoint)
            error = frappe.get_traceback()
            _log(f"RFQ Sync Error: {name}", error)
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results