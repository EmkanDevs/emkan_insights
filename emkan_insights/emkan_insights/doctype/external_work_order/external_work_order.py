import frappe
from frappe.model.document import Document
import json
from frappe.utils import cint, flt


class ExternalWorkOrder(Document):
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

# Parent-level links on Work Order
WORK_ORDER_PARENT_LINKS = {
    "custom_c_production_plan": "Production Plan",
    "sales_order": "Sales Order",
    "bom_no": "BOM",
    "production_plan": "Production Plan",
    "project": "Project",
}

COMPANY_PREFIXED_PARENT_FIELDS = {
    "custom_c_production_plan",
    "bom_no",
    "sales_order",
}

# Child-level links on Work Order Item (required_items)
WORK_ORDER_ITEM_LINKS = {
    "bom_no": "BOM",
    "sales_order": "Sales Order",
}

# Child-level links on Work Order Operation
WORK_ORDER_OPERATION_LINKS = {
    "operation": "Operation",
    "workstation": "Workstation",
    "workstation_type": "Workstation Type",
    "bom": "BOM",
}

# If one of these fields exists on External Work Order,
# local Work Order name will be written back there.
EXTERNAL_LINK_BACK_FIELDS = (
    "work_order",
    "work_order_name",
    "local_work_order",
    "synced_work_order",
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
        frappe.throw("Missing base id for Work Order naming")

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


def _apply_parent_link_mapping(wo_doc, external, company_abbr):
    for fieldname, linked_doctype in WORK_ORDER_PARENT_LINKS.items():
        if not _doctype_has_field("Work Order", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            continue

        if fieldname in COMPANY_PREFIXED_PARENT_FIELDS:
            wo_doc.set(fieldname, _prefix_reference(company_abbr, raw_value))
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            wo_doc.set(fieldname, resolved)
        else:
            wo_doc.set(fieldname, None)
            frappe.log_error(
                title=f"Work Order parent link unresolved: {external.name}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _apply_required_item_link_mapping(item_row, company_abbr, external_name=None):
    for fieldname, linked_doctype in WORK_ORDER_ITEM_LINKS.items():
        raw_value = item_row.get(fieldname)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            item_row[fieldname] = resolved
        else:
            item_row[fieldname] = None
            frappe.log_error(
                title=f"Work Order item link unresolved: {external_name or 'Unknown'}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _apply_operation_link_mapping(op_row, company_abbr, external_name=None):
    for fieldname, linked_doctype in WORK_ORDER_OPERATION_LINKS.items():
        raw_value = op_row.get(fieldname)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            op_row[fieldname] = resolved
        else:
            op_row[fieldname] = None
            frappe.log_error(
                title=f"Work Order operation link unresolved: {external_name or 'Unknown'}",
                message=f"Field: {fieldname}\nIncoming: {raw_value}\nAbbr: {company_abbr}"
            )


def _resolve_sales_order_item(item_row):
    """
    If sales_order was resolved locally, resolve sales_order_item
    using local parent + item_code if possible.
    """
    so_name = item_row.get("sales_order")
    item_code = item_row.get("item_code")

    if not so_name or not item_code:
        return

    so_item_name = frappe.db.get_value(
        "Sales Order Item",
        {"parent": so_name, "item_code": item_code},
        "name"
    )

    if so_item_name:
        item_row["sales_order_item"] = so_item_name


def _write_back_external_link(external_name, target_name):
    meta = frappe.get_meta("External Work Order")
    for fieldname in EXTERNAL_LINK_BACK_FIELDS:
        if meta.has_field(fieldname):
            frappe.db.set_value("External Work Order", external_name, fieldname, target_name, update_modified=False)
            return fieldname
    return None


def _make_required_item_fingerprint(item_row):
    def safe_float(val):
        return round(flt(val), 3)

    return (
        str(item_row.get("item_code") or "").strip().lower(),
        str(item_row.get("source_warehouse") or "").strip().lower(),
        safe_float(item_row.get("required_qty") or item_row.get("qty")),
    )


def _make_operation_fingerprint(op_row):
    def safe_float(val):
        return round(flt(val), 3)

    return (
        str(op_row.get("operation") or "").strip().lower(),
        str(op_row.get("workstation") or "").strip().lower(),
        safe_float(op_row.get("time_in_mins")),
    )


def _make_scrap_item_fingerprint(scrap_row):
    def safe_float(val):
        return round(flt(val), 3)

    return (
        str(scrap_row.get("item_code") or "").strip().lower(),
        safe_float(scrap_row.get("qty")),
    )


def _append_required_items(wo_doc, external, company_abbr):
    wo_doc.set("required_items", [])
    if not hasattr(external, "required_items") or not external.required_items:
        return

    seen = set()
    for row in external.required_items:
        item_row = {}
        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                item_row[field] = value

        if item_row.get("item_code"):
            item_row["item_code"] = _resolve_item_code(item_row["item_code"])

        _apply_required_item_link_mapping(item_row, company_abbr, external.name)
        _resolve_sales_order_item(item_row)

        item_row = _filter_valid_fields("Work Order Item", item_row)

        fingerprint = _make_required_item_fingerprint(item_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        wo_doc.append("required_items", item_row)


def _append_operations(wo_doc, external, company_abbr):
    wo_doc.set("operations", [])
    if not hasattr(external, "operations") or not external.operations:
        if _doctype_has_field("Work Order", "operations"):
            wo_doc.set("operations", [])
        return

    seen = set()
    for row in external.operations:
        op_row = {}
        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS:
                op_row[field] = value

        _apply_operation_link_mapping(op_row, company_abbr, external.name)
        op_row = _filter_valid_fields("Work Order Operation", op_row)

        fingerprint = _make_operation_fingerprint(op_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        wo_doc.append("operations", op_row)


def _append_scrap_items(wo_doc, external, company_abbr):
    wo_doc.set("scrap_items", [])
    if not hasattr(external, "scrap_items") or not external.scrap_items:
        return

    if not frappe.db.exists("DocType", "Work Order Scrap Item"):
        return

    seen = set()
    for row in external.scrap_items:
        scrap_row = {}
        for field, value in row.as_dict().items():
            if field not in SYSTEM_FIELDS:
                scrap_row[field] = value

        if scrap_row.get("item_code"):
            scrap_row["item_code"] = _resolve_item_code(scrap_row["item_code"])

        scrap_row = _filter_valid_fields("Work Order Scrap Item", scrap_row)

        fingerprint = _make_scrap_item_fingerprint(scrap_row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        wo_doc.append("scrap_items", scrap_row)


def _dedupe_work_order_children(wo_name):
    """Remove exact-duplicate child rows that may have slipped in via update-on-insert or hooks."""
    def _dedupe_table(doctype, parentfield, key_fields, float_fields=()):
        if not frappe.db.exists("DocType", doctype):
            return
        rows = frappe.db.get_all(
            doctype,
            filters={"parent": wo_name, "parenttype": "Work Order", "parentfield": parentfield},
            fields=["name"] + key_fields + list(float_fields),
            order_by="creation ASC",
        )
        seen = set()
        to_delete = []
        for r in rows:
            key = []
            for kf in key_fields:
                key.append(str(r.get(kf) or "").strip().lower())
            for ff in float_fields:
                key.append(round(flt(r.get(ff)), 3))
            key = tuple(key)
            if key in seen:
                to_delete.append(r.name)
            else:
                seen.add(key)
        if to_delete:
            frappe.db.delete(doctype, {"name": ["in", to_delete]})

    _dedupe_table(
        "Work Order Item", "required_items",
        ["item_code", "source_warehouse"],
        ["required_qty"],
    )
    _dedupe_table(
        "Work Order Operation", "operations",
        ["operation", "workstation", "workstation_type"],
        ["time_in_mins"],
    )
    _dedupe_table(
        "Work Order Scrap Item", "scrap_items",
        ["item_code"],
        ["qty"],
    )


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_work_order(external_name, company):
    lock_key = f"sync_ewo:{external_name}"
    if not frappe.cache().set(lock_key, "1", ex=600, nx=True):
        frappe.logger().warning(f"Sync already running for {external_name}, skipping")
        return {"name": external_name, "status": "locked"}

    try:
        return _do_sync_external_work_order(external_name, company)
    finally:
        frappe.cache().delete(lock_key)


def _do_sync_external_work_order(external_name, company):
    external = frappe.get_doc("External Work Order", external_name)

    company_abbr = _get_company_abbr(company)
    base_id = external.remote_id or external.name
    target_name = _build_target_name(company_abbr, base_id)

    # Check if it already exists locally
    if frappe.db.exists("Work Order", target_name):
        _write_back_external_link(external_name, target_name)
        result = _sync_docstatus(target_name, external)
        result["status"] = "exists"
        return result

    wo = frappe.new_doc("Work Order")

    if _doctype_has_field("Work Order", "remote_id"):
        wo.remote_id = base_id

    if hasattr(wo, "source_site"):
        wo.source_site = getattr(external, "source_site", None)

    # Copy header fields
    source_data = external.as_dict()
    for field, value in source_data.items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ("required_items", "operations", "scrap_items", "remote_id", "production_item", "item_code"):
            continue

        if _doctype_has_field("Work Order", field):
            wo.set(field, value)

    wo.company = company

    # Resolve the main Work Order Item (ERPNext uses 'production_item')
    raw_item = getattr(external, "production_item", None) or getattr(external, "item_code", None)
    if raw_item:
        wo.production_item = _resolve_item_code(raw_item)

    if wo.production_item and not wo.get("item_name"):
        wo.item_name = frappe.db.get_value("Item", wo.production_item, "item_name")
        
    if wo.production_item and not wo.get("stock_uom"):
        wo.stock_uom = frappe.db.get_value("Item", wo.production_item, "stock_uom")

    # Resolve parent links
    _apply_parent_link_mapping(wo, external, company_abbr)

    # Child Tables
    _append_required_items(wo, external, company_abbr)
    _append_operations(wo, external, company_abbr)
    _append_scrap_items(wo, external, company_abbr)

    # IMPORTANT: Force deterministic local name (No dirty SQL updates required)
    wo.flags.name_set = True
    wo.name = target_name

    wo.flags.ignore_permissions = True
    wo.flags.ignore_validate = True
    wo.flags.ignore_mandatory = True
    wo.flags.ignore_links = True
    wo.flags.ignore_naming_series = True

    # Temporarily clear BOM No to prevent ERPNext's before_validate hook 
    # from auto-appending BOM items and causing duplicates
    original_bom_no = wo.bom_no
    wo.bom_no = None

    try:
        wo.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
    except Exception:
        frappe.log_error(title=f"Work Order Insert FAILED: {external_name}", message=frappe.get_traceback())
        raise

    # Restore BOM No immediately after insert
    if original_bom_no:
        frappe.db.set_value("Work Order", target_name, "bom_no", original_bom_no, update_modified=False)

    # Defensive: kill any duplicates that may have been appended by an update-on-insert or hook
    _dedupe_work_order_children(target_name)

    _write_back_external_link(external_name, target_name)

    if _doctype_has_field("Work Order", "remote_id"):
        frappe.db.set_value("Work Order", target_name, "remote_id", base_id, update_modified=False)

    result = _sync_docstatus(target_name, external)
    result["status"] = "synced"
    return result


def _sync_docstatus(target_name, external):
    """Mirror external docstatus/status onto the local Work Order. Idempotent — safe to call every sync."""
    wo_status = (external.get("status") or "Draft").strip()
    external_docstatus = cint(external.docstatus)

    if external_docstatus == 1:
        target_docstatus = 1
    elif external_docstatus == 2:
        target_docstatus = 2
    elif wo_status == "Cancelled":
        target_docstatus = 2
    elif wo_status == "Completed":
        target_docstatus = 1
    elif wo_status in ("In Process", "Open", "Submitted"):
        target_docstatus = 1
    else:
        target_docstatus = 0

    wo = frappe.get_doc("Work Order", target_name)
    wo.flags.ignore_permissions = True
    wo.flags.ignore_validate = True
    wo.flags.ignore_mandatory = True
    wo.flags.ignore_links = True
    wo.flags.ignore_naming_series = True
    wo.flags.name_set = True

    current_docstatus = cint(wo.docstatus)
    submit_exception = None

    try:
        if target_docstatus == 0 and current_docstatus == 0:
            pass
        elif target_docstatus == 1 and current_docstatus == 0:
            wo.submit()
            _dedupe_work_order_children(target_name)
        elif target_docstatus == 2:
            if current_docstatus == 0:
                wo.submit()
                _dedupe_work_order_children(target_name)
                wo = frappe.get_doc("Work Order", target_name)
                wo.flags.ignore_permissions = True
                wo.flags.ignore_validate = True
                wo.flags.ignore_mandatory = True
                wo.flags.ignore_links = True
                wo.flags.ignore_naming_series = True
                wo.flags.name_set = True
            if wo.docstatus == 1:
                wo.cancel()
                _dedupe_work_order_children(target_name)
        elif target_docstatus == 1 and current_docstatus == 1:
            pass
    except Exception as submit_err:
        submit_exception = submit_err
        frappe.log_error(
            title=f"Work Order Submit/Cancel Error: {target_name}",
            message=frappe.get_traceback()
        )

    if submit_exception or cint(frappe.db.get_value("Work Order", target_name, "docstatus")) != target_docstatus:
        frappe.db.commit()
        frappe.db.set_value("Work Order", target_name, "docstatus", target_docstatus, update_modified=False)
        for child_doctype in ("Work Order Item", "Work Order Operation", "Work Order Scrap Item"):
            if frappe.db.exists("DocType", child_doctype):
                frappe.db.set_value(
                    child_doctype,
                    {"parent": target_name, "parenttype": "Work Order"},
                    "docstatus", target_docstatus, update_modified=False
                )
        frappe.db.commit()

    if wo_status and wo_status != "Draft":
        frappe.db.set_value("Work Order", target_name, "status", wo_status, update_modified=False)

    if wo_status == "Completed" and frappe.db.exists("DocType", "Work Order Operation"):
        frappe.db.set_value(
            "Work Order Operation",
            {"parent": target_name, "parenttype": "Work Order"},
            "status", "Completed",
            update_modified=False
        )

    actual_item_count = frappe.db.count("Work Order Item", {"parent": target_name})

    return {
        "name": target_name,
        "docstatus": target_docstatus,
        "wo_status": wo_status,
        "required_items_count": actual_item_count
    }


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_work_order_docs(source_doctype=None, names=None):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            external_doc = frappe.get_doc(source_doctype or "External Work Order", name)
            result = sync_external_work_order(name, external_doc.company)
            results.append(result)

        except Exception as e:
            frappe.log_error(
                title=f"Work Order Sync Error: {name}",
                message=frappe.get_traceback()
            )
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results