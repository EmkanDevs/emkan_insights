import frappe
import json
from frappe.utils import cint, flt

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


def _resolve_quotation(remote_qtn_name, company_abbr=None):
    
    if not remote_qtn_name:
        return None

    remote_qtn_name = str(remote_qtn_name).strip()
    if not remote_qtn_name:
        return None

    candidates = [remote_qtn_name]
    
    if company_abbr and not remote_qtn_name.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_qtn_name}")

    seen = set()
    unique = []
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)

    for name in unique:
        if frappe.db.exists("Quotation", name):
            return name

    meta = frappe.get_meta("Quotation")
    for name in unique:
        for field in ("remote_id", "custom_remote_id"):
            if meta.has_field(field):
                found = frappe.db.get_value("Quotation", {field: name}, "name")
                if found:
                    return found

    for name in unique:
        if frappe.db.exists("External Quotation", name):
            linked = frappe.db.get_value("External Quotation", name, "remote_id")
            if linked and frappe.db.exists("Quotation", linked):
                return linked

    return None


def _resolve_quotation_item(quotation_name, remote_item_id=None, item_code=None):
    """Map remote quotation_item id → local Quotation Item name."""
    if not quotation_name:
        return None

    if remote_item_id and frappe.get_meta("Quotation Item").has_field("custom_remote_id"):
        found = frappe.db.get_value(
            "Quotation Item",
            {"parent": quotation_name, "custom_remote_id": remote_item_id},
            "name",
        )
        if found:
            return found

    if item_code:
        return frappe.db.get_value(
            "Quotation Item",
            {"parent": quotation_name, "item_code": item_code},
            "name",
        )

    return None


def _resolve_amended_from(remote_amended_from, company_abbr=None):
    
    if not remote_amended_from:
        return None

    remote_amended_from = str(remote_amended_from).strip()
    if not remote_amended_from:
        return None

    candidates = [remote_amended_from]
    if company_abbr and not remote_amended_from.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_amended_from}")

    local_name = None
    for name in candidates:
        if frappe.db.exists("Sales Order", name):
            local_name = name
            break
        found = frappe.db.get_value("Sales Order", {"remote_id": name}, "name")
        if found:
            local_name = found
            break

    if not local_name:
        return None

    if cint(frappe.db.get_value("Sales Order", local_name, "docstatus")) != 2:
        return None

    return local_name


def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message or "")


def _set_docstatus_sql(doctype, docname, docstatus, status):
    """Force docstatus on parent + child tables (Stock Entry sync pattern)."""
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
    """Force cancel via SQL when normal cancel() fails (same as Stock Entry sync)."""
    _set_docstatus_sql(doctype, docname, 2, "Cancelled")


def _force_submit_doc(doctype, docname):
    """Force submit via SQL when validate/on_submit blocks (e.g. Quotation qty limit)."""
    _set_docstatus_sql(doctype, docname, 1, "To Deliver and Bill")


def _safe_cancel_doc(doc, log_prefix=""):
    """Cancel with force-cancel fallback (same as Stock Entry sync)."""
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


def _safe_submit_doc(doc, log_prefix=""):
    """Submit with force-submit fallback when Quotation over-limit etc. blocks."""
    try:
        doc.flags.ignore_permissions = True
        doc.flags.ignore_validate = True
        doc.flags.ignore_mandatory = True
        doc.flags.ignore_links = True
        doc.submit()
        frappe.db.commit()
        return True
    except Exception as submit_err:
        # submit() may have already written docstatus=1 before on_submit threw
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


def _sync_existing_docstatus(existing_name, external):
    """
    Sync docstatus for an already-existing Sales Order.
    Fixes submitted External SOs that were left as local Draft because
    re-sync previously returned status=exists without updating docstatus.
    Same pattern as external_stock_entry_sync._sync_existing_docstatus.
    """
    local_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
    target_docstatus = cint(external.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists", "docstatus": local_docstatus}

    try:
        so = frappe.get_doc("Sales Order", existing_name)

        if target_docstatus == 2:
            if local_docstatus == 0:
                _safe_submit_doc(so, log_prefix=f"Sales Order {existing_name}")
                so = frappe.get_doc("Sales Order", existing_name)

            _safe_cancel_doc(so, log_prefix=f"Sales Order {existing_name}")
            final_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
            if final_docstatus == 2:
                return {"name": existing_name, "status": "docstatus_synced", "docstatus": 2}
            return {
                "name": existing_name,
                "status": "exists",
                "warning": f"cancel verification failed - docstatus={final_docstatus}",
            }

        if target_docstatus == 1 and local_docstatus == 0:
            _safe_submit_doc(so, log_prefix=f"Sales Order {existing_name}")
            final_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
            if final_docstatus == 1:
                return {"name": existing_name, "status": "docstatus_synced", "docstatus": 1}
            return {
                "name": existing_name,
                "status": "exists",
                "warning": f"submit verification failed - docstatus={final_docstatus}",
            }

        _log(
            f"Sales Order {existing_name} unhandled docstatus transition",
            f"local={local_docstatus}, external={target_docstatus}",
        )
        return {
            "name": existing_name,
            "status": "exists",
            "warning": "unhandled transition",
            "docstatus": local_docstatus,
        }

    except Exception as trans_err:
        # If a prior partial submit already landed, treat as synced
        final_docstatus = cint(frappe.db.get_value("Sales Order", existing_name, "docstatus"))
        if final_docstatus == target_docstatus:
            frappe.db.commit()
            return {"name": existing_name, "status": "docstatus_synced", "docstatus": final_docstatus}

        _log(f"Sales Order {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(trans_err)}


@frappe.whitelist()
def sync_external_sales_order_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_so = frappe.get_doc(source_doctype, name)
            remote_id = ext_so.name

            # ------------------------------------------------
            # BUILD TARGET NAME: {company_abbr}-{remote_id}
            # Same pattern as Work Order sync
            # ------------------------------------------------
            company_abbr = frappe.db.get_value('Company', ext_so.company, 'abbr') or ''
            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # CHECK EXISTING (by remote_id, same as Work Order)
            # ------------------------------------------------
            existing_so = frappe.db.get_value(
                "Sales Order",
                {"remote_id": remote_id},
                "name"
            ) or (target_name if frappe.db.exists("Sales Order", target_name) else None)

            if existing_so:
                # Same as Stock Entry: still sync docstatus on re-sync
                results.append(_sync_existing_docstatus(existing_so, ext_so))
                continue

            so = frappe.new_doc("Sales Order")

            # ------------------------------------------------
            # SET NAME WITH COMPANY ABBR PREFIX
            # Same pattern as Work Order
            # ------------------------------------------------
            so.remote_id = remote_id
            so.name = target_name
            so.flags.name_set = True

            src_site_value = getattr(ext_so, "site_url", None) or getattr(ext_so, "source_site", None)
            if src_site_value:
                mapped = False
                for site_field in ("source_site", "site_url"):
                    if hasattr(so, site_field):
                        so.set(site_field, src_site_value)
                        mapped = True
                        break
                if not mapped:
                    frappe.log_error(
                        f"SO Sync: source_site value '{src_site_value}' not mapped — "
                        f"Sales Order missing 'source_site'/'site_url' Custom Field.",
                        "SO Sync: missing source_site field"
                    )

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            so.flags.ignore_permissions = True
            so.flags.ignore_mandatory = True
            so.flags.ignore_links = True
            so.flags.ignore_validate = True
            so.flags.ignore_naming_series = True

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # NOTE: All child-table fields (items, taxes,
            # payment_schedule, etc.) must be excluded here and
            # rebuilt explicitly below. Otherwise Frappe tries to
            # reuse the source doc's child row names, which
            # already exist in the DB -> DuplicateEntryError.
            # ------------------------------------------------
            for field, value in ext_so.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in [
                        "items", "taxes", "payment_schedule",
                        "remote_id", "amended_from",
                    ]
                    and hasattr(so, field)
                ):
                    so.set(field, value)

            # Resolve amended_from to local cancelled SO (prefix / remote_id).
            # Blind copy of remote amended_from fails validate_amended_from.
            so.amended_from = _resolve_amended_from(
                getattr(ext_so, "amended_from", None), company_abbr
            )

            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
            so.set("items", [])
            conversion_rate = flt(getattr(ext_so, "conversion_rate", 0)) or 1.0

            for row in ext_so.items:
                item_code = row.get("item_code")

                if not frappe.db.exists("Item", item_code):
                    frappe.get_doc({
                        "doctype": "Item",
                        "item_code": item_code,
                        "item_name": row.get("item_name") or item_code,
                        "item_group": "All Item Groups",
                        "stock_uom": row.get("uom") or "Nos"
                    }).insert(ignore_permissions=True)

                item_name = (
                    frappe.db.get_value("Item", item_code, "item_name")
                    or row.get("item_name")
                    or item_code
                )

                qty    = flt(row.get("qty")) or 1
                rate   = flt(row.get("rate")) or 0
                amount = flt(row.get("amount")) if flt(row.get("amount")) else qty * rate

                # prevdoc_docname = remote Quotation (e.g. SAL-QTN-…);
                # resolve to local prefixed Quotation (e.g. IMC-SAL-QTN-…)
                prevdoc_docname = _resolve_quotation(
                    row.get("prevdoc_docname"), company_abbr
                )
                quotation_item = None
                if prevdoc_docname:
                    quotation_item = _resolve_quotation_item(
                        prevdoc_docname,
                        remote_item_id=row.get("quotation_item"),
                        item_code=item_code,
                    )

                so.append("items", {
                    "item_code":            item_code,
                    "item_name":            item_name,
                    "description":          row.get("description") or item_name,
                    "custom_remote_id":     row.get("custom_remote_id"),  
                    "qty":                  qty,
                    "stock_qty":            qty,
                    "rate":                 rate,
                    "amount":               amount,
                    "net_rate":             flt(row.get("net_rate")) or rate,
                    "net_amount":           flt(row.get("net_amount")) or amount,
                    "base_rate":            rate * conversion_rate,
                    "base_amount":          amount * conversion_rate,
                    "base_net_rate":        (flt(row.get("net_rate")) or rate) * conversion_rate,
                    "base_net_amount":      (flt(row.get("net_amount")) or amount) * conversion_rate,
                    "price_list_rate":      flt(row.get("price_list_rate")),
                    "base_price_list_rate": flt(row.get("base_price_list_rate")),
                    "discount_percentage":  flt(row.get("discount_percentage")),
                    "discount_amount":      flt(row.get("discount_amount")),
                    "item_tax_rate":        row.get("item_tax_rate") or "{}",
                    "uom":                  row.get("uom") or "Nos",
                    "stock_uom":            row.get("stock_uom") or row.get("uom") or "Nos",
                    "conversion_factor":    row.get("conversion_factor") or 1,
                    "warehouse":            row.get("warehouse") or DEFAULT_WAREHOUSE,
                    "delivery_date":        row.get("delivery_date"),
                    "cost_center":          row.get("cost_center"),
                    "project":              row.get("project") or getattr(ext_so, "project", None),
                    "prevdoc_docname":      prevdoc_docname,
                    "quotation_item":       quotation_item,
                })

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_so, "taxes"):
                so.set("taxes", [])
                for row in ext_so.taxes:
                    so.append("taxes", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })

            # ------------------------------------------------
            # PAYMENT SCHEDULE
            # Rebuilt explicitly (like taxes) so that stale child
            # row names from the source doc are stripped out and
            # Frappe generates fresh names for the new doc.
            # ------------------------------------------------
            if hasattr(ext_so, "payment_schedule"):
                so.set("payment_schedule", [])
                for row in ext_so.payment_schedule:
                    so.append("payment_schedule", {
                        f: v for f, v in row.as_dict().items()
                        if f not in SYSTEM_FIELDS
                    })
            # ------------------------------------------------
            so.total                = flt(getattr(ext_so, "total", 0))
            so.net_total             = flt(getattr(ext_so, "net_total", 0)) or so.total
            so.base_total            = flt(getattr(ext_so, "base_total", 0))
            so.base_net_total        = flt(getattr(ext_so, "base_net_total", 0)) or so.base_total
            so.total_qty             = flt(getattr(ext_so, "total_qty", 0))
            so.grand_total           = flt(getattr(ext_so, "grand_total", 0))
            so.base_grand_total      = flt(getattr(ext_so, "base_grand_total", 0))
            so.total_taxes_and_charges      = flt(getattr(ext_so, "total_taxes_and_charges", 0))
            so.base_total_taxes_and_charges = flt(getattr(ext_so, "base_total_taxes_and_charges", 0))
            so.rounding_adjustment          = flt(getattr(ext_so, "rounding_adjustment", 0))
            so.base_rounding_adjustment      = flt(getattr(ext_so, "base_rounding_adjustment", 0))
            so.rounded_total                 = flt(getattr(ext_so, "rounded_total", 0))
            so.base_rounded_total            = flt(getattr(ext_so, "base_rounded_total", 0))
            so.in_words                      = getattr(ext_so, "in_words", "")
            so.base_in_words                 = getattr(ext_so, "base_in_words", "")
            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            so.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # ------------------------------------------------
            # NAMING SAFETY NET: If Frappe renamed it
            # Same pattern as Work Order
            # ------------------------------------------------
            if so.name != target_name:

                if not frappe.db.exists("Sales Order", target_name):
                    frappe.db.sql("""
                        UPDATE `tabSales Order`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, so.name))

                old_name = so.name

                child_tables = [
                    "Sales Order Item",
                    "Sales Order Tax",
                    "Payment Schedule",
                ]

                for child_table in child_tables:
                    if frappe.db.exists("DocType", child_table):
                        frappe.db.sql("""
                            UPDATE `tab{0}`
                            SET parent = %s
                            WHERE parent = %s
                        """.format(child_table), (target_name, old_name))

                so.name = target_name

            frappe.db.commit()

            # ------------------------------------------------
            # VERIFY: confirm items actually landed
            # ------------------------------------------------
            actual_item_count = frappe.db.count("Sales Order Item", {"parent": target_name})
            expected_item_count = len(ext_so.get("items") or [])

            if actual_item_count != expected_item_count:
                frappe.log_error(
                    f"Sales Order {target_name}: expected {expected_item_count} "
                    f"items, found {actual_item_count} after insert.",
                    "ExternalSalesOrder Sync - items mismatch"
                )

            # ------------------------------------------------
            # DOCSTATUS SYNC (same pattern as Stock Entry sync)
            # ------------------------------------------------
            target_status = cint(ext_so.docstatus)
            frappe.clear_document_cache("Sales Order", target_name)
            updated_so = frappe.get_doc("Sales Order", target_name)

            if target_status == 1:
                try:
                    _safe_submit_doc(updated_so, log_prefix=f"Sales Order {target_name}")
                    results.append({
                        "name": target_name,
                        "status": "synced",
                        "docstatus": 1,
                        "items_count": actual_item_count,
                    })
                except Exception as submit_err:
                    _log(f"Sales Order {target_name} SUBMIT FAILED",
                         frappe.get_traceback())
                    results.append({
                        "name": target_name,
                        "status": "synced_draft",
                        "docstatus": cint(frappe.db.get_value("Sales Order", target_name, "docstatus")),
                        "error": f"Submit failed: {str(submit_err)}",
                        "items_count": actual_item_count,
                    })

            elif target_status == 2:
                try:
                    _safe_submit_doc(updated_so, log_prefix=f"Sales Order {target_name}")
                    updated_so = frappe.get_doc("Sales Order", target_name)
                    _safe_cancel_doc(updated_so, log_prefix=f"Sales Order {target_name}")

                    final_status = cint(frappe.db.get_value("Sales Order", target_name, "docstatus"))
                    results.append({
                        "name": target_name,
                        "status": "synced" if final_status == 2 else "sync_error",
                        "docstatus": final_status,
                        "items_count": actual_item_count,
                    })
                except Exception as cancel_err:
                    _log(f"Sales Order {target_name} CANCEL FAILED",
                         frappe.get_traceback())
                    try:
                        _force_cancel_doc("Sales Order", target_name)
                        frappe.db.commit()
                        results.append({
                            "name": target_name,
                            "status": "synced",
                            "docstatus": 2,
                            "items_count": actual_item_count,
                        })
                    except Exception:
                        results.append({
                            "name": target_name,
                            "status": "synced_submitted",
                            "error": f"Cancel failed: {str(cancel_err)}",
                            "items_count": actual_item_count,
                        })
            else:
                results.append({
                    "name": target_name,
                    "status": "synced",
                    "docstatus": 0,
                    "items_count": actual_item_count,
                })

            frappe.db.commit()

        except Exception as e:
            frappe.db.rollback()

            frappe.log_error(
                title=f"SO Sync Fail: {name}",
                message=frappe.get_traceback()
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results