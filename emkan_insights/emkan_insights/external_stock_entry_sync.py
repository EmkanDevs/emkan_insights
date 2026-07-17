# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document
from frappe.utils import cint, flt


class ExternalStockEntry(Document):
    pass


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation", "quotation_item", "prevdoc_doctype",
    "prevdoc_docname", "so_detail", "against_sales_order"
}


# ==========================================================
# PARENT-LEVEL LINKED FIELDS on Stock Entry
# ==========================================================
STOCK_ENTRY_PARENT_LINKS = {
    "work_order": "Work Order",
    "purchase_order": "Purchase Order",
    "subcontracting_order": "Subcontracting Order",
    "delivery_note_no": "Delivery Note",
    "sales_invoice_no": "Sales Invoice",
    "pick_list": "Pick List",
    "purchase_receipt_no": "Purchase Receipt",
    "asset_repair": "Asset Repair",
    "bom_no": "BOM",
    "job_card": "Job Card",
    "project": "Project",
    "credit_note": "Stock Entry",
    "outgoing_stock_entry": "Stock Entry",
    "stock_entry_type": "Stock Entry Type",
    "from_warehouse": "Warehouse",
    "to_warehouse": "Warehouse",
    "supplier": "Supplier",
    "supplier_address": "Address",
    "source_warehouse_address": "Address",
    "target_warehouse_address": "Address",
    "select_print_heading": "Print Heading",
    "letter_head": "Letter Head",
}

COMPANY_PREFIXED_PARENT_FIELDS = {
    "work_order",
    "purchase_order",
    "bom_no",
    "job_card",
    "outgoing_stock_entry",
    "credit_note",
}


# ==========================================================
# CHILD-LEVEL LINKED FIELDS on Stock Entry Detail
# ==========================================================
STOCK_ENTRY_ITEM_LINKS = {
    "s_warehouse": "Warehouse",
    "t_warehouse": "Warehouse",
    "item_code": "Item",
    "quality_inspection": "Quality Inspection",
    "subcontracted_item": "Item",
    "uom": "UOM",
    "stock_uom": "UOM",
    "serial_and_batch_bundle": "Serial and Batch Bundle",
    "batch_no": "Batch",
    "expense_account": "Account",
    "cost_center": "Cost Center",
    "project": "Project",
    "bom_no": "BOM",
    "material_request": "Material Request",
    "material_request_item": "Material Request Item",
    "original_item": "Item",
    "against_stock_entry": "Stock Entry",
    "putaway_rule": "Putaway Rule",
    "reference_purchase_receipt": "Purchase Receipt",
}

COMPANY_PREFIXED_ITEM_FIELDS = {
    "bom_no",
    "against_stock_entry",
    "reference_purchase_receipt",
}


# ==========================================================
# FORCE CANCEL & SAFE CANCEL HELPERS
# ==========================================================

def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


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


# ==========================================================
# OTHER HELPERS
# ==========================================================

def _get_company_abbr(company):
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(f"Company '{company}' has no abbreviation set")
    return abbr


def _build_target_name(company_abbr, base_id):
    base_id = (base_id or "").strip()
    if not base_id:
        frappe.throw("Missing base id for Stock Entry naming")

    if base_id.startswith(f"{company_abbr}-"):
        return base_id

    return f"{company_abbr}-{base_id}"


def _prefix_reference(company_abbr, value):
    value = str(value or "").strip()
    if not value:
        return None

    if value.startswith(f"{company_abbr}-"):
        return value

    return f"{company_abbr}-{value}"


def _doctype_has_field(doctype, fieldname):
    meta = frappe.get_meta(doctype)
    return meta.has_field(fieldname) or fieldname in meta.get_valid_columns()


def _filter_valid_fields(doctype, values):
    meta = frappe.get_meta(doctype)
    valid_fields = set(meta.get_valid_columns())
    return {k: v for k, v in values.items() if k in valid_fields}


def _resolve_link_with_prefix(doctype, remote_name, company_abbr=None):
    """
    Resolve a linked document reference by trying company-prefixed name first.
    """
    if not remote_name:
        return None

    remote_name = str(remote_name).strip()
    if not remote_name:
        return None

    # 1. Already prefixed
    if company_abbr and remote_name.startswith(f"{company_abbr}-"):
        if frappe.db.exists(doctype, remote_name):
            return remote_name

    # 2. Try prefixed version
    if company_abbr:
        prefixed = f"{company_abbr}-{remote_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    # 3. Try exact match
    if frappe.db.exists(doctype, remote_name):
        return remote_name

    # 4. Try remote_id lookup
    meta = frappe.get_meta(doctype)
    for field in ("remote_id", "custom_remote_id"):
        if meta.has_field(field):
            local_name = frappe.db.get_value(doctype, {field: remote_name}, "name")
            if local_name:
                return local_name

    # 5. Not found
    return None


def _resolve_item_code(item_code):
    """Resolve item code via External Item mapping if needed."""
    if not item_code:
        return item_code

    if frappe.db.exists("Item", item_code):
        return item_code

    # Try External Item mapping
    mapped = frappe.db.get_value("External Item", item_code, "remote_id")
    if mapped:
        return mapped

    external_item_meta = frappe.get_meta("External Item")
    for field in ("remote_id", "custom_remote_id"):
        if external_item_meta.has_field(field):
            mapped_name = frappe.db.get_value("External Item", {field: item_code}, "remote_id")
            if mapped_name:
                return mapped_name

    return item_code


def _resolve_material_request(remote_mr_name, company_abbr=None):
    """
    Same pattern as PO/SQ/RFQ link resolvers:
    1. remote_id / custom_remote_id on Material Request
    2. Double-prefix local name ({abbr}-{remote_name})
    3. Bridge via External Material Request.remote_id
    4. Exact local name
    """
    if not remote_mr_name:
        return None

    remote_mr_name = str(remote_mr_name).strip()
    if not remote_mr_name:
        return None

    candidates = [remote_mr_name]
    if company_abbr and not remote_mr_name.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_mr_name}")

    # 1. remote_id / custom_remote_id (authoritative — MR sync stores remote name here)
    for name in candidates:
        for field in ("remote_id", "custom_remote_id"):
            if _doctype_has_field("Material Request", field):
                found = frappe.db.get_value("Material Request", {field: name}, "name")
                if found:
                    return found

    # 2. Prefixed / exact local name
    for name in candidates:
        if frappe.db.exists("Material Request", name):
            return name

    # 3. Bridge via External Material Request → local MR name
    for name in candidates:
        if frappe.db.exists("External Material Request", name):
            linked = frappe.db.get_value("External Material Request", name, "remote_id")
            if linked and frappe.db.exists("Material Request", linked):
                return linked

    return None


def _remap_material_request_links(stock_entry_name, external, company_abbr):
    
    if not stock_entry_name or not external:
        return

    external_by_item = {}
    if getattr(external, "items", None):
        for row in external.items:
            item_code = _resolve_item_code(getattr(row, "item_code", None))
            remote_mr = getattr(row, "material_request", None)
            if item_code and remote_mr:
                external_by_item[item_code] = remote_mr

    local_rows = frappe.get_all(
        "Stock Entry Detail",
        filters={"parent": stock_entry_name},
        fields=["name", "item_code", "material_request"],
    )

    for local_row in local_rows:
        remote_mr = external_by_item.get(local_row.item_code) or local_row.material_request
        if not remote_mr:
            continue

        resolved_mr = _resolve_material_request(remote_mr, company_abbr)
        if not resolved_mr:
            continue

        mr_item = frappe.db.get_value(
            "Material Request Item",
            {"parent": resolved_mr, "item_code": local_row.item_code},
            "name",
        )

        values = {}
        if resolved_mr != local_row.material_request:
            values["material_request"] = resolved_mr
        if mr_item:
            values["material_request_item"] = mr_item

        if values:
            frappe.db.set_value(
                "Stock Entry Detail",
                local_row.name,
                values,
                update_modified=False,
            )


def _apply_parent_link_mapping(se_doc, external, company_abbr):
    """Resolve and prefix all parent-level linked fields on Stock Entry."""
    for fieldname, linked_doctype in STOCK_ENTRY_PARENT_LINKS.items():
        if not _doctype_has_field("Stock Entry", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            continue

        # Fields that should always be prefixed
        if fieldname in COMPANY_PREFIXED_PARENT_FIELDS:
            se_doc.set(fieldname, _prefix_reference(company_abbr, raw_value))
            continue

        # Other links: try resolve with prefix first
        resolved = _resolve_link_with_prefix(linked_doctype, raw_value, company_abbr)
        if resolved:
            se_doc.set(fieldname, resolved)
        else:
            se_doc.set(fieldname, None)
            frappe.log_error(
                title=f"Stock Entry parent link unresolved: {external.name}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _apply_item_link_mapping(item_row, company_abbr, external_name=None):
    """Resolve and prefix all child-level linked fields on Stock Entry Detail rows."""
    for fieldname, linked_doctype in STOCK_ENTRY_ITEM_LINKS.items():
        raw_value = item_row.get(fieldname)
        if not raw_value:
            continue

        # Fields that should always be prefixed
        if fieldname in COMPANY_PREFIXED_ITEM_FIELDS:
            item_row[fieldname] = _prefix_reference(company_abbr, raw_value)
            continue

        # Item code special handling
        if fieldname == "item_code":
            item_row[fieldname] = _resolve_item_code(raw_value)
            continue

        # Material Request: resolve by remote_id first
        if fieldname == "material_request":
            resolved = _resolve_material_request(raw_value, company_abbr)
            if resolved:
                item_row[fieldname] = resolved
            else:
                item_row[fieldname] = None
                frappe.log_error(
                    title=f"Stock Entry item link unresolved: {external_name or 'Unknown'}",
                    message=f"Field: material_request\nIncoming: {raw_value}\nAbbr: {company_abbr}"
                )
            continue

        # Other links: try resolve with prefix first
        resolved = _resolve_link_with_prefix(linked_doctype, raw_value, company_abbr)
        if resolved:
            item_row[fieldname] = resolved
        else:
            item_row[fieldname] = None
            frappe.log_error(
                title=f"Stock Entry item link unresolved: {external_name or 'Unknown'}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _resolve_material_request_item(item_row):
    """Resolve material_request_item using local parent + item_code."""
    mr_name = item_row.get("material_request")
    item_code = item_row.get("item_code")

    if not mr_name or not item_code:
        return

    mr_item_name = frappe.db.get_value(
        "Material Request Item",
        {"parent": mr_name, "item_code": item_code},
        "name"
    )

    if mr_item_name:
        item_row["material_request_item"] = mr_item_name


def _make_item_fingerprint(item_row):
    """Create a unique fingerprint for deduplication."""
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(item_row.get("item_code") or "").strip(),
        str(item_row.get("s_warehouse") or "").strip(),
        str(item_row.get("t_warehouse") or "").strip(),
        safe_float(item_row.get("qty")),
        str(item_row.get("batch_no") or "").strip(),
        str(item_row.get("serial_no") or "").strip(),
    )


def _deduplicate_child_table(parent_name, child_table, key_fields):
    """Remove duplicate child rows based on key fields."""
    rows = frappe.get_all(
        child_table,
        filters={"parent": parent_name},
        fields=["name", "creation"] + list(key_fields),
        order_by="creation asc"
    )

    seen = set()
    to_delete = []

    for row in rows:
        key = tuple(str(row.get(f) or "").strip() for f in key_fields)
        if key in seen:
            to_delete.append(row.name)
        else:
            seen.add(key)

    for name in to_delete:
        frappe.db.sql(f"DELETE FROM `tab{child_table}` WHERE name = %s", (name,))

    if to_delete:
        frappe.db.commit()


def _safe_dedupe(parent_name):
    """Safely deduplicate Stock Entry child tables."""
    try:
        _deduplicate_child_table(
            parent_name,
            "Stock Entry Detail",
            ("item_code", "s_warehouse", "t_warehouse", "qty", "batch_no", "serial_no")
        )
    except Exception:
        _log(
            title=f"Stock Entry dedupe failed for {parent_name}",
            message=frappe.get_traceback(),
        )


# ==========================================================
# HANDLE EXISTING DOC STATUS SYNC
# ==========================================================

def _sync_existing_docstatus(existing_name, external):
    """
    Handle docstatus sync for already-existing Stock Entry documents.
    """
    local_docstatus = frappe.db.get_value("Stock Entry", existing_name, "docstatus")
    target_docstatus = cint(external.docstatus)

    if local_docstatus == target_docstatus:
        return {"name": existing_name, "status": "exists"}

    try:
        se = frappe.get_doc("Stock Entry", existing_name)
        se.flags.ignore_permissions = True
        se.flags.ignore_validate = True
        se.flags.ignore_mandatory = True
        se.flags.ignore_links = True

        if target_docstatus == 2:
            # External is cancelled - we need to cancel local too
            if local_docstatus == 0:
                # Draft locally -> submit first, then cancel
                se.submit()
                frappe.db.commit()
                se = frappe.get_doc("Stock Entry", existing_name)
            
            # Now cancel (whether it was 0 or 1 before)
            _safe_cancel_doc(se, log_prefix=f"Stock Entry {existing_name}")
            _safe_dedupe(se.name)
            frappe.db.commit()
            
            # Verify
            final_docstatus = frappe.db.get_value("Stock Entry", se.name, "docstatus")
            if final_docstatus == 2:
                return {"name": se.name, "status": "docstatus_synced", "docstatus": 2}
            else:
                _log(f"Stock Entry {se.name} existing doc cancel verification failed",
                     f"Expected docstatus=2, got {final_docstatus}")
                return {"name": se.name, "status": "exists", "warning": "cancel verification failed"}

        elif target_docstatus == 1 and local_docstatus == 0:
            se.submit()
            frappe.db.commit()
            _safe_dedupe(se.name)
            frappe.db.commit()
            return {"name": se.name, "status": "docstatus_synced", "docstatus": 1}

        else:
            _log(f"Stock Entry {existing_name} unhandled docstatus transition",
                f"local={local_docstatus}, external={target_docstatus}")
            return {"name": existing_name, "status": "exists", "warning": "unhandled transition"}

    except Exception as trans_err:
        _log(f"Stock Entry {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(trans_err)}


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_stock_entry(external_name, company):
    external = frappe.get_doc("External Stock Entry", external_name)

    company_abbr = _get_company_abbr(company)
    base_id = external.name
    target_name = _build_target_name(company_abbr, base_id)

    # Check if it already exists locally
    existing = frappe.db.get_value(
        "Stock Entry", {"remote_id": base_id}, "name"
    ) or (target_name if frappe.db.exists("Stock Entry", target_name) else None)

    if existing:
        # Existing SE path only syncs docstatus — still remap MR links by remote_id
        try:
            _remap_material_request_links(existing, external, company_abbr)
            frappe.db.commit()
        except Exception:
            _log(
                f"Stock Entry {existing} material_request remap failed",
                frappe.get_traceback(),
            )
        return _sync_existing_docstatus(existing, external)

    se = frappe.new_doc("Stock Entry")

    if _doctype_has_field("Stock Entry", "remote_id"):
        se.remote_id = base_id

    if hasattr(se, "source_site"):
        se.source_site = getattr(external, "source_site", None)

    # Copy header fields (exclude child tables and link fields)
    source_data = external.as_dict()
    for field, value in source_data.items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ("items", "additional_costs", "remote_id", "stock_entry_type"):
            continue
        if field in STOCK_ENTRY_PARENT_LINKS:
            continue

        if _doctype_has_field("Stock Entry", field):
            se.set(field, value)

    se.company = company

    # Resolve parent-level links with company prefix
    _apply_parent_link_mapping(se, external, company_abbr)

    # ==========================================================
    # CHILD TABLE: items (Stock Entry Detail)
    # ==========================================================
    se.set("items", [])
    if hasattr(external, "items") and external.items:
        seen = set()
        for row in external.items:
            item_row = {}
            for field, value in row.as_dict().items():
                if field in SYSTEM_FIELDS:
                    continue
                if field in IGNORE_ITEM_FIELDS:
                    continue
                item_row[field] = value

            # Resolve all linked fields with company prefix
            _apply_item_link_mapping(item_row, company_abbr, external.name)

            # Resolve material_request_item if material_request was resolved
            _resolve_material_request_item(item_row)

            # Filter to only valid fields
            item_row = _filter_valid_fields("Stock Entry Detail", item_row)

            # Deduplicate
            fingerprint = _make_item_fingerprint(item_row)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            se.append("items", item_row)

    # ==========================================================
    # CHILD TABLE: additional_costs (Landed Cost Taxes and Charges)
    # ==========================================================
    se.set("additional_costs", [])
    if hasattr(external, "additional_costs") and external.additional_costs:
        for row in external.additional_costs:
            cost_row = {}
            for field, value in row.as_dict().items():
                if field in SYSTEM_FIELDS:
                    continue
                cost_row[field] = value

            cost_row = _filter_valid_fields("Landed Cost Taxes and Charges", cost_row)
            se.append("additional_costs", cost_row)

    # ==========================================================
    # LOG PRE-INSERT STATE
    # ==========================================================
    _log(f"Stock Entry {target_name} pre-insert check",
         f"Items: {len(se.items)}, Stock Entry Type: {se.stock_entry_type}, "
         f"Company: {se.company}, External docstatus: {external.docstatus}")

    # ==========================================================
    # INSERT with target name set BEFORE insert
    # ==========================================================
    se.flags.name_set = True
    se.name = target_name
    se.flags.ignore_permissions = True
    se.flags.ignore_validate = True
    se.flags.ignore_mandatory = True
    se.flags.ignore_links = True
    se.flags.ignore_naming_series = True

    try:
        se.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
    except Exception:
        frappe.log_error(title=f"Stock Entry Insert FAILED: {external_name}", message=frappe.get_traceback())
        raise

    # Ensure remote_id is set
    if _doctype_has_field("Stock Entry", "remote_id"):
        frappe.db.set_value("Stock Entry", se.name, "remote_id", base_id, update_modified=False)

    # ==========================================================
    # NAMING SAFETY NET
    # ==========================================================
    if se.name != target_name:
        if not frappe.db.exists("Stock Entry", target_name):
            frappe.db.sql("""
                UPDATE `tabStock Entry`
                SET name = %s
                WHERE name = %s
            """, (target_name, se.name))

        old_name = se.name
        for child_table in ["Stock Entry Detail", "Landed Cost Taxes and Charges"]:
            if frappe.db.exists("DocType", child_table):
                frappe.db.sql("""
                    UPDATE `tab{0}`
                    SET parent = %s
                    WHERE parent = %s
                """.format(child_table), (target_name, old_name))

        se.name = target_name

    # Write back name to External Stock Entry remote_id field
    ext_meta = frappe.get_meta("External Stock Entry")
    if ext_meta.has_field("remote_id"):
        frappe.db.set_value(
            "External Stock Entry", external_name,
            "remote_id", target_name,
            update_modified=False
        )

    frappe.db.commit()
    _safe_dedupe(target_name)
    frappe.db.commit()

    # Always remap MR links after insert (draft + submitted).
    # Draft rows previously kept the remote MR name because existing-only
    # remap never ran until a later re-sync.
    try:
        _remap_material_request_links(se.name, external, company_abbr)
        frappe.db.commit()
    except Exception:
        _log(
            f"Stock Entry {se.name} material_request post-insert remap failed",
            frappe.get_traceback(),
        )

    _log(f"Stock Entry {se.name} inserted successfully",
         f"Docstatus after insert: {se.docstatus}")

    # ==========================================================
    # DOCSTATUS SYNC - KEY FIX HERE
    # ==========================================================
    target_docstatus = cint(external.docstatus)

    frappe.clear_document_cache("Stock Entry", se.name)
    se = frappe.get_doc("Stock Entry", se.name)

    _log(f"Stock Entry {se.name} pre-docstatus-sync",
         f"External docstatus: {target_docstatus}, Current docstatus: {se.docstatus}")

    if target_docstatus == 1:
        # Submit the Stock Entry
        try:
            se.flags.ignore_permissions = True
            se.flags.ignore_validate = True
            se.flags.ignore_mandatory = True
            se.flags.ignore_links = True
            se.submit()
            frappe.db.commit()
            _safe_dedupe(se.name)
            frappe.db.commit()
            _log(f"Stock Entry {se.name} submitted successfully", "")
            
            actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
            return {
                "name": se.name,
                "status": "synced",
                "docstatus": 1,
                "items_count": actual_item_count
            }

        except Exception as submit_err:
            tb = frappe.get_traceback()
            _log(f"Stock Entry {se.name} SUBMIT FAILED",
                 f"Error: {str(submit_err)}\n\nTraceback:\n{tb}")
            actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
            return {
                "name": se.name,
                "status": "synced_draft",
                "error": f"Submit failed: {str(submit_err)}",
                "items_count": actual_item_count
            }

    elif target_docstatus == 2:
        # ============================================================
        # KEY FIX: Handle cancelled external Stock Entry properly
        # ============================================================
        try:
            se.flags.ignore_permissions = True
            se.flags.ignore_validate = True
            se.flags.ignore_mandatory = True
            se.flags.ignore_links = True
            
            # Submit first (required before cancel)
            se.submit()
            frappe.db.commit()
            _log(f"Stock Entry {se.name} submitted (step 1 of cancel)", "")

            # Re-fetch and cancel with safe cancel (includes force fallback)
            se = frappe.get_doc("Stock Entry", se.name)
            _safe_cancel_doc(se, log_prefix=f"Stock Entry {se.name}")
            
            _safe_dedupe(se.name)
            frappe.db.commit()
            
            # Verify the cancel worked
            final_docstatus = frappe.db.get_value("Stock Entry", se.name, "docstatus")
            final_status = frappe.db.get_value("Stock Entry", se.name, "status")
            
            _log(f"Stock Entry {se.name} cancel complete",
                 f"Final docstatus: {final_docstatus}, Final status: {final_status}")
            
            actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
            
            if final_docstatus == 2:
                return {
                    "name": se.name,
                    "status": "synced",
                    "docstatus": 2,
                    "items_count": actual_item_count
                }
            else:
                _log(f"Stock Entry {se.name} CANCEL VERIFICATION FAILED",
                     f"Expected docstatus=2, got docstatus={final_docstatus}")
                return {
                    "name": se.name,
                    "status": "sync_error",
                    "error": f"Cancel verification failed - docstatus is {final_docstatus}",
                    "items_count": actual_item_count
                }

        except Exception as cancel_err:
            tb = frappe.get_traceback()
            _log(f"Stock Entry {se.name} CANCEL FAILED - CRITICAL",
                 f"Error: {str(cancel_err)}\n\nTraceback:\n{tb}")
            
            # Last resort: Force set docstatus directly
            try:
                _force_cancel_doc("Stock Entry", se.name)
                frappe.db.commit()
                _log(f"Stock Entry {se.name} force cancelled as last resort", "")
                
                actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
                return {
                    "name": se.name,
                    "status": "synced",
                    "docstatus": 2,
                    "items_count": actual_item_count
                }
            except Exception as force_err:
                _log(f"Stock Entry {se.name} FORCE CANCEL ALSO FAILED",
                     f"Force error: {str(force_err)}")
                
                actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
                return {
                    "name": se.name,
                    "status": "synced_submitted",
                    "error": f"Cancel failed: {str(cancel_err)}",
                    "items_count": actual_item_count
                }

    else:
        # docstatus = 0 (Draft) - already in correct state
        actual_item_count = frappe.db.count("Stock Entry Detail", {"parent": se.name})
        return {
            "name": se.name,
            "status": "synced",
            "docstatus": 0,
            "items_count": actual_item_count
        }


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_stock_entry_docs(source_doctype=None, names=None):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            frappe.db.commit()  # Reset any partial transaction state
            
            external_doc = frappe.get_doc(source_doctype or "External Stock Entry", name)
            result = sync_external_stock_entry(name, external_doc.company)
            results.append(result)

        except Exception as e:
            frappe.log_error(
                title=f"Stock Entry Sync Error: {name}",
                message=frappe.get_traceback()
            )
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results