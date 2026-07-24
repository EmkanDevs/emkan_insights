import frappe
import json
from frappe.model.document import Document
from frappe.utils import cint, flt
from contextlib import contextmanager


class ExternalStockEntry(Document):
    pass


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation", "quotation_item", "prevdoc_doctype",
    "prevdoc_docname", "so_detail", "against_sales_order",
    "last_scanned_warehouse",
}


# ==========================================================
# SAVEPOINT CONTEXT MANAGER
# ==========================================================

@contextmanager
def savepoint_context(name):
    safe_name = frappe.scrub(name)[:50]
    frappe.db.savepoint(safe_name)
    try:
        yield
    except Exception:
        frappe.db.rollback(save_point=safe_name)
        raise


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
        doc.cancel()  # ORM cancel writes to DB

        _log(f"{log_prefix} cancelled normally", "")
        return True
    except Exception as normal_cancel_err:
        _log(f"{log_prefix} normal cancel failed, attempting force cancel",
             f"Error: {str(normal_cancel_err)}")

        # Fallback: Force cancel via SQL
        try:
            _force_cancel_doc(doc.doctype, doc.name)
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

    double_prefix = f"{company_abbr}-{company_abbr}-"
    if base_id.startswith(double_prefix):
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
    meta_columns = set(frappe.get_meta(doctype).get_valid_columns())
    db_columns = set(frappe.db.get_table_columns(doctype))
    valid_fields = meta_columns & db_columns
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

        if target_docstatus == 2:
            # External is cancelled - we need to cancel local too
            if local_docstatus == 0:
                # Draft locally -> submit first, then cancel
                se.submit()  # ORM submit writes to DB
                se = frappe.get_doc("Stock Entry", existing_name)

            # Now cancel (whether it was 0 or 1 before)
            _safe_cancel_doc(se, log_prefix=f"Stock Entry {existing_name}")
            _safe_dedupe(se.name)

            # Verify
            final_docstatus = frappe.db.get_value("Stock Entry", se.name, "docstatus")
            if final_docstatus == 2:
                return {"name": se.name, "status": "docstatus_synced", "docstatus": 2}
            else:
                _log(f"Stock Entry {se.name} existing doc cancel verification failed",
                     f"Expected docstatus=2, got {final_docstatus}")
                return {"name": se.name, "status": "exists", "warning": "cancel verification failed"}

        elif target_docstatus == 1 and local_docstatus == 0:
            se.submit()  # ORM submit writes to DB
            _safe_dedupe(se.name)
            return {"name": se.name, "status": "docstatus_synced", "docstatus": 1}

        else:
            _log(f"Stock Entry {existing_name} unhandled docstatus transition",
                f"local={local_docstatus}, external={target_docstatus}")
            return {"name": existing_name, "status": "exists", "warning": "unhandled transition"}

    except Exception as trans_err:
        _log(f"Stock Entry {existing_name} docstatus transition failed", frappe.get_traceback())
        return {"name": existing_name, "status": "exists", "error": str(trans_err)}


# ==========================================================
# BUILD HEADER VALUES FOR DIRECT SQL INSERT
# ==========================================================

def _build_header_values(external, company, company_abbr):
    """Build a dict of valid header field values for direct SQL insert."""
    header = {}

    # Copy simple fields (exclude child tables, system fields, link fields)
    source_data = external.as_dict()
    for field, value in source_data.items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ("items", "additional_costs", "remote_id", "stock_entry_type"):
            continue
        if field in STOCK_ENTRY_PARENT_LINKS:
            continue
        if not _doctype_has_field("Stock Entry", field):
            continue
        header[field] = value

    # Override company
    header["company"] = company

    # Resolve parent-level links with company prefix
    for fieldname, linked_doctype in STOCK_ENTRY_PARENT_LINKS.items():
        if not _doctype_has_field("Stock Entry", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            header[fieldname] = None
            continue

        if fieldname in COMPANY_PREFIXED_PARENT_FIELDS:
            header[fieldname] = _prefix_reference(company_abbr, raw_value)
            continue

        resolved = _resolve_link_with_prefix(linked_doctype, raw_value, company_abbr)
        if resolved:
            header[fieldname] = resolved
        else:
            header[fieldname] = None

    # Set remote_id to the EXTERNAL name (not the local name)
    if _doctype_has_field("Stock Entry", "remote_id"):
        header["remote_id"] = (external.remote_id or external.name or "").strip()

    if _doctype_has_field("Stock Entry", "source_site"):
        header["source_site"] = getattr(external, "source_site", None)

    # Filter to only valid columns
    header = _filter_valid_fields("Stock Entry", header)

    return header


# ==========================================================
# BUILD CHILD ROW VALUES
# ==========================================================

def _build_child_rows(external, company_abbr):
    """Build deduplicated child row dicts for Stock Entry Detail."""
    rows = []
    seen = set()

    if not hasattr(external, "items") or not external.items:
        return rows

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

        rows.append(item_row)

    return rows


def _build_additional_cost_rows(external):
    """Build child row dicts for Landed Cost Taxes and Charges."""
    rows = []

    if not hasattr(external, "additional_costs") or not external.additional_costs:
        return rows

    for row in external.additional_costs:
        cost_row = {}
        for field, value in row.as_dict().items():
            if field in SYSTEM_FIELDS:
                continue
            cost_row[field] = value

        cost_row = _filter_valid_fields("Landed Cost Taxes and Charges", cost_row)
        rows.append(cost_row)

    return rows


# ==========================================================
# DIRECT SQL INSERT HELPERS (NO COMMIT)
# ==========================================================

def _direct_insert_stock_entry(target_name, header_values, item_rows, cost_rows):
    """
    Insert Stock Entry and child tables directly via SQL.
    No savepoints, no rollback calls — relies on Frappe's automatic
    transaction rollback when the exception propagates.
    """
    
    # ── Collision check ──────────────────────────────────────────
    if frappe.db.exists("Stock Entry", target_name):
        raise frappe.ValidationError(
            _("Stock Entry {0} already exists. Cannot create duplicate.").format(target_name)
        )

    now = frappe.utils.now()
    user = frappe.session.user

    # ── Insert parent: Stock Entry ───────────────────────────────
    meta = frappe.get_meta("Stock Entry")
    valid_columns = set(frappe.db.get_table_columns("Stock Entry"))

    header_values["name"] = target_name
    header_values["creation"] = now
    header_values["modified"] = now
    header_values["owner"] = user
    header_values["modified_by"] = user
    header_values["docstatus"] = 0
    header_values["idx"] = 0

    fields = [f for f in header_values.keys() if f in valid_columns]
    placeholders = ", ".join(["%s"] * len(fields))
    values = [header_values.get(f) for f in fields]

    frappe.db.sql(
        f"INSERT INTO `tabStock Entry` ({', '.join(fields)}) VALUES ({placeholders})",
        values
    )

    # ── Insert children: Stock Entry Detail ──────────────────────
    for idx, item_row in enumerate(item_rows, start=1):
        child_name = frappe.generate_hash("", 10)
        item_row["name"] = child_name
        item_row["parent"] = target_name
        item_row["parenttype"] = "Stock Entry"
        item_row["parentfield"] = "items"
        item_row["idx"] = idx
        item_row["creation"] = now
        item_row["modified"] = now
        item_row["owner"] = user
        item_row["modified_by"] = user
        item_row["docstatus"] = 0

        child_meta = frappe.get_meta("Stock Entry Detail")
        child_valid = set(frappe.db.get_table_columns("Stock Entry Detail"))
        child_fields = [f for f in item_row.keys() if f in child_valid]
        child_placeholders = ", ".join(["%s"] * len(child_fields))
        child_values = [item_row.get(f) for f in child_fields]

        frappe.db.sql(
            f"INSERT INTO `tabStock Entry Detail` ({', '.join(child_fields)}) VALUES ({child_placeholders})",
            child_values
        )

    # ── Insert children: Landed Cost Taxes and Charges ───────────
    for idx, cost_row in enumerate(cost_rows, start=1):
        child_name = frappe.generate_hash("", 10)
        cost_row["name"] = child_name
        cost_row["parent"] = target_name
        cost_row["parenttype"] = "Stock Entry"
        cost_row["parentfield"] = "additional_costs"
        cost_row["idx"] = idx
        cost_row["creation"] = now
        cost_row["modified"] = now
        cost_row["owner"] = user
        cost_row["modified_by"] = user
        cost_row["docstatus"] = 0

        child_meta = frappe.get_meta("Landed Cost Taxes and Charges")
        child_valid = set(frappe.db.get_table_columns("Landed Cost Taxes and Charges"))
        child_fields = [f for f in cost_row.keys() if f in child_valid]
        child_placeholders = ", ".join(["%s"] * len(child_fields))
        child_values = [cost_row.get(f) for f in child_fields]

        frappe.db.sql(
            f"INSERT INTO `tabLanded Cost Taxes and Charges` ({', '.join(child_fields)}) VALUES ({child_placeholders})",
            child_values
        )

    return target_name


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_stock_entry(external_name, company):
    external = frappe.get_doc("External Stock Entry", external_name)

    company_abbr = _get_company_abbr(company)
    source_stock_entry_id = (external.remote_id or external.name or "").strip()
    target_name = _build_target_name(company_abbr, source_stock_entry_id)

    existing = frappe.db.get_value(
        "Stock Entry", {"remote_id": source_stock_entry_id}, "name"
    ) or (target_name if frappe.db.exists("Stock Entry", target_name) else None)

    if existing:
        # Existing SE path only syncs docstatus — still remap MR links by remote_id
        try:
            _remap_material_request_links(existing, external, company_abbr)
        except Exception:
            _log(
                f"Stock Entry {existing} material_request remap failed",
                frappe.get_traceback(),
            )
        return _sync_existing_docstatus(existing, external)

    # ==========================================================
    # CREATE PATH: Direct SQL insert to bypass naming series
    # ==========================================================

    # Build header values
    header_values = _build_header_values(external, company, company_abbr)

    # Build child rows
    item_rows = _build_child_rows(external, company_abbr)
    cost_rows = _build_additional_cost_rows(external)

    # Log pre-insert state
    _log(f"Stock Entry {target_name} pre-insert check",
         f"Items: {len(item_rows)}, Stock Entry Type: {header_values.get('stock_entry_type')}, "
         f"Company: {company}, External docstatus: {external.docstatus}")

    # Direct SQL insert — bypasses naming series completely
    _direct_insert_stock_entry(target_name, header_values, item_rows, cost_rows)

    # Write back name to External Stock Entry remote_id field via ORM
    ext_meta = frappe.get_meta("External Stock Entry")
    # if ext_meta.has_field("remote_id"):
        # external.db_set("remote_id", target_name, update_modified=False)

    _safe_dedupe(target_name)

    # Always remap MR links after insert (draft + submitted).
    try:
        _remap_material_request_links(target_name, external, company_abbr)
    except Exception:
        _log(
            f"Stock Entry {target_name} material_request post-insert remap failed",
            frappe.get_traceback(),
        )

    _log(f"Stock Entry {target_name} inserted successfully",
         f"Docstatus after insert: 0")

    # ==========================================================
    # DOCSTATUS SYNC
    # ==========================================================
    target_docstatus = cint(external.docstatus)

    frappe.clear_document_cache("Stock Entry", target_name)
    se = frappe.get_doc("Stock Entry", target_name)

    _log(f"Stock Entry {se.name} pre-docstatus-sync",
         f"External docstatus: {target_docstatus}, Current docstatus: {se.docstatus}")

    if target_docstatus == 1:
        # Submit the Stock Entry
        try:
            se.flags.ignore_permissions = True
            se.submit()  # ORM submit writes to DB
            _safe_dedupe(se.name)
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
        # Handle cancelled external Stock Entry
        try:
            se.flags.ignore_permissions = True

            # Submit first (required before cancel)
            se.submit()  # ORM submit writes to DB
            _log(f"Stock Entry {se.name} submitted (step 1 of cancel)", "")

            # Re-fetch and cancel with safe cancel (includes force fallback)
            se = frappe.get_doc("Stock Entry", se.name)
            _safe_cancel_doc(se, log_prefix=f"Stock Entry {se.name}")

            _safe_dedupe(se.name)

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
            # If sync_external_stock_entry fails midway (e.g. after insert but before submit),
            # the savepoint rolls back the partial insert cleanly, preventing ghost Draft Stock Entries
            # from polluting the next iteration.
            with savepoint_context(f'sync_se_{name}'):
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