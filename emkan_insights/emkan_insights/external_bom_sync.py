# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

from frappe.model.document import Document
import json
import frappe
from frappe.utils import cint, flt


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation", "quotation_item", "prevdoc_doctype",
    "prevdoc_docname", "so_detail", "against_sales_order"
}

# Parent-level links on BOM
BOM_PARENT_LINKS = {
    "project": "Project",
    "routing": "Routing",
}

# Child-level links on BOM Item (Raw Materials)
BOM_ITEM_LINKS = {
    "bom_no": "BOM",  # Self-referential link for nested BOMs
}

# Child-level links on BOM Operation
BOM_OPERATION_LINKS = {
    "operation": "Operation",
    "workstation": "Workstation",
    "workstation_type": "Workstation Type",
}

# If one of these fields exists on External BOM,
# local BOM name will be written back there.
EXTERNAL_LINK_BACK_FIELDS = (
    "bom",
    "bom_name",
    "local_bom",
    "synced_bom",
)


# ==========================================================
# HELPERS
# ==========================================================

def _parse_names(names):
    if not names:
        return []

    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    return list(names)


def _get_company_abbr(company):
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(f"Company '{company}' has no abbreviation set")
    return abbr


def _build_target_name(company_abbr, base_id):
    base_id = (base_id or "").strip()
    if not base_id:
        frappe.throw("Missing base id for BOM naming")

    # If it already has the prefix, return as is
    if base_id.startswith(f"{company_abbr}-"):
        return base_id

    # Add the company prefix
    return f"{company_abbr}-{base_id}"


def _doctype_has_field(doctype, fieldname):
    meta = frappe.get_meta(doctype)
    return meta.has_field(fieldname) or fieldname in meta.get_valid_columns()


def _filter_valid_fields(doctype, values):
    meta = frappe.get_meta(doctype)
    valid_fields = set(meta.get_valid_columns())
    return {k: v for k, v in values.items() if k in valid_fields}


def _resolve_link_with_abbr(doctype, remote_name, company_abbr=None):
    if not remote_name:
        return None

    remote_name = str(remote_name).strip()
    if not remote_name:
        return None

    if company_abbr and remote_name.startswith(f"{company_abbr}-"):
        if frappe.db.exists(doctype, remote_name):
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


def _resolve_item_code(item_code):
    if not item_code:
        return item_code

    if frappe.db.exists("Item", item_code):
        return item_code

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


def _apply_parent_link_mapping(bom_doc, external, company_abbr):
    for fieldname, linked_doctype in BOM_PARENT_LINKS.items():
        if not _doctype_has_field("BOM", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            bom_doc.set(fieldname, resolved)
        else:
            frappe.log_error(
                title=f"BOM parent link unresolved: {external.name}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _apply_item_link_mapping(item_row, company_abbr, external_name=None):
    for fieldname, linked_doctype in BOM_ITEM_LINKS.items():
        raw_value = item_row.get(fieldname)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            item_row[fieldname] = resolved
        else:
            # If BOM cannot be found locally, null it out to prevent validation errors
            item_row[fieldname] = None
            frappe.log_error(
                title=f"BOM item link unresolved: {external_name or 'Unknown'}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _apply_operation_link_mapping(op_row, company_abbr, external_name=None):
    for fieldname, linked_doctype in BOM_OPERATION_LINKS.items():
        raw_value = op_row.get(fieldname)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            op_row[fieldname] = resolved
        else:
            op_row[fieldname] = None
            frappe.log_error(
                title=f"BOM operation link unresolved: {external_name or 'Unknown'}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _ensure_stock_uom(item_row):
    if not item_row.get("stock_uom") and item_row.get("item_code"):
        item_row["stock_uom"] = frappe.db.get_value("Item", item_row["item_code"], "stock_uom")
    if not item_row.get("stock_uom"):
        item_row["stock_uom"] = "Nos"


def _write_back_external_link(external_name, target_name):
    meta = frappe.get_meta("External BOM")
    for fieldname in EXTERNAL_LINK_BACK_FIELDS:
        if meta.has_field(fieldname):
            frappe.db.set_value("External BOM", external_name, fieldname, target_name, update_modified=False)
            return fieldname
    return None


def _make_bom_item_fingerprint(item_row):
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(item_row.get("item_code") or "").strip(),
        str(item_row.get("bom_no") or "").strip(),
        safe_float(item_row.get("qty")),
        str(item_row.get("uom") or "").strip(),
        str(item_row.get("stock_uom") or "").strip(),
        safe_float(item_row.get("rate")),
    )


def _make_bom_operation_fingerprint(op_row):
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(op_row.get("operation") or "").strip(),
        str(op_row.get("workstation") or "").strip(),
        safe_float(op_row.get("time_in_mins")),
    )


def _make_bom_scrap_item_fingerprint(scrap_row):
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(scrap_row.get("item_code") or "").strip(),
        safe_float(scrap_row.get("qty")),
    )


def _append_bom_items(bom_doc, external, company_abbr):
    bom_doc.set("items", [])
    if not hasattr(external, "items") or not external.items:
        return

    seen = set()
    for row in external.items:
        item_row = {}
        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                item_row[field] = value

        if item_row.get("item_code"):
            item_row["item_code"] = _resolve_item_code(item_row["item_code"])

        _apply_item_link_mapping(item_row, company_abbr, external.name)
        _ensure_stock_uom(item_row)

        item_row = _filter_valid_fields("BOM Item", item_row)

        fingerprint = _make_bom_item_fingerprint(item_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        
        bom_doc.append("items", item_row)


def _append_bom_operations(bom_doc, external, company_abbr):
    bom_doc.set("operations", [])
    if not hasattr(external, "operations") or not external.operations:
        if _doctype_has_field("BOM", "with_operations"):
            bom_doc.with_operations = 0
        return

    if _doctype_has_field("BOM", "with_operations"):
        bom_doc.with_operations = 1

    seen = set()
    for row in external.operations:
        op_row = {}
        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS:
                op_row[field] = value

        _apply_operation_link_mapping(op_row, company_abbr, external.name)
        op_row = _filter_valid_fields("BOM Operation", op_row)

        fingerprint = _make_bom_operation_fingerprint(op_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        bom_doc.append("operations", op_row)


def _append_bom_scrap_items(bom_doc, external, company_abbr):
    bom_doc.set("scrap_items", [])
    if not hasattr(external, "scrap_items") or not external.scrap_items:
        return

    seen = set()
    for row in external.scrap_items:
        scrap_row = {}
        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS:
                scrap_row[field] = value

        if scrap_row.get("item_code"):
            scrap_row["item_code"] = _resolve_item_code(scrap_row["item_code"])
            
        _ensure_stock_uom(scrap_row)
        scrap_row = _filter_valid_fields("BOM Scrap Item", scrap_row)

        fingerprint = _make_bom_scrap_item_fingerprint(scrap_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        bom_doc.append("scrap_items", scrap_row)


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_bom(external_name, company):
    external = frappe.get_doc("External BOM", external_name)

    company_abbr = _get_company_abbr(company)
    base_id = external.remote_id or external.name
    target_name = _build_target_name(company_abbr, base_id)

    # Check if it already exists locally by the prefixed name
    if frappe.db.exists("BOM", target_name):
        _write_back_external_link(external_name, target_name)
        return {"name": target_name, "status": "exists"}

    bom_doc = frappe.new_doc("BOM")

    if _doctype_has_field("BOM", "remote_id"):
        bom_doc.remote_id = base_id

    if hasattr(bom_doc, "source_site"):
        bom_doc.source_site = getattr(external, "source_site", None)

    # Copy header fields
    source_data = external.as_dict()
    for field, value in source_data.items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ("items", "operations", "scrap_items", "exploded_items", "remote_id", "item", "item_code"):
            continue

        if _doctype_has_field("BOM", field):
            bom_doc.set(field, value)

    bom_doc.company = company

    # Resolve the main BOM Item. 
    # Note: The parent BOM doctype uses `item`, while child table uses `item_code`
    raw_item = getattr(external, "item", None) or getattr(external, "item_code", None)
    if raw_item:
        bom_doc.item = _resolve_item_code(raw_item)
        
    if bom_doc.item and not bom_doc.get("item_name"):
        bom_doc.item_name = frappe.db.get_value("Item", bom_doc.item, "item_name")
        
    if bom_doc.item and not bom_doc.get("uom"):
        bom_doc.uom = frappe.db.get_value("Item", bom_doc.item, "stock_uom")
        
    if bom_doc.item and not bom_doc.get("stock_uom"):
        bom_doc.stock_uom = bom_doc.uom

    _apply_parent_link_mapping(bom_doc, external, company_abbr)

    _append_bom_items(bom_doc, external, company_abbr)
    _append_bom_operations(bom_doc, external, company_abbr)
    _append_bom_scrap_items(bom_doc, external, company_abbr)

    # IMPORTANT: Force deterministic local name (No dirty SQL updates required)
    bom_doc.flags.name_set = True
    bom_doc.name = target_name

    bom_doc.flags.ignore_permissions = True
    bom_doc.flags.ignore_validate = True
    bom_doc.flags.ignore_mandatory = True
    bom_doc.flags.ignore_links = True

    try:
        bom_doc.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
    except Exception:
        frappe.log_error(title=f"BOM Insert FAILED: {external_name}", message=frappe.get_traceback())
        raise

    _write_back_external_link(external_name, target_name)

    if _doctype_has_field("BOM", "remote_id"):
        frappe.db.set_value("BOM", target_name, "remote_id", base_id, update_modified=False)

    if cint(external.docstatus) == 1:
        submitted_doc = frappe.get_doc("BOM", target_name)
        submitted_doc.flags.ignore_permissions = True
        submitted_doc.submit()

    return {"name": target_name, "status": "synced"}


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_bom_docs(source_doctype=None, names=None):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            external_doc = frappe.get_doc(source_doctype or "External BOM", name)
            result = sync_external_bom(name, external_doc.company)
            results.append(result)

        except Exception as e:
            frappe.log_error(
                title=f"BOM Sync Error: {name}",
                message=frappe.get_traceback()
            )
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results