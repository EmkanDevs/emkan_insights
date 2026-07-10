import frappe
from frappe.model.document import Document
import json
from frappe.utils import cint, flt


class ExternalWorkOrder(Document):
    pass

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }

# IGNORE_ITEM_FIELDS = {
#     # "quotation",
#     # "quotation_item",
#     # "prevdoc_doctype",
#     # "prevdoc_docname",
#     # "so_detail",
#     # "against_sales_order"
# }

# DEFAULT_WAREHOUSE = "Stores - IMC"


# @frappe.whitelist()
# def sync_external_work_order_docs(source_doctype, names):

#     if isinstance(names, str):
#         names = json.loads(names)

#     results = []

#     for name in names:
#         try:
#             ext_wo = frappe.get_doc(source_doctype, name)

#             # ------------------------------------------------
#             # CHECK EXISTING
#             # ------------------------------------------------
#             existing_wo = frappe.db.get_value(
#                 "Work Order",
#                 {"remote_id": ext_wo.remote_id},
#                 "name"
#             )

#             if existing_wo:
#                 results.append({
#                     "name": existing_wo,
#                     "status": "exists"
#                 })
#                 continue

#             wo = frappe.new_doc("Work Order")

#             target_name = ext_wo.remote_id
#             wo.remote_id = target_name
#             wo.name = target_name = f"{frappe.db.get_value('Company', ext_wo.company, 'abbr')}-{ext_wo.remote_id}"
#             wo.flags.name_set = True

#             if hasattr(wo, "source_site"):
#                 wo.source_site = ext_wo.source_site

#             # ------------------------------------------------
#             # COPY MAIN FIELDS
#             # ------------------------------------------------
#             for field, value in ext_wo.as_dict().items():

#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "operations", "scrap_items", "required_items", "remote_id"]
#                     and hasattr(wo, field)
#                 ):
#                     wo.set(field, value)

#             # ------------------------------------------------
#             # REQUIRED ITEMS (Raw Materials for Work Order)
#             # ------------------------------------------------
#             if hasattr(ext_wo, "required_items"):

#                 wo.set("required_items", [])

#                 for row in ext_wo.required_items:

#                     item_row = {}

#                     for field, value in row.as_dict().items():

#                         if (
#                             field not in SYSTEM_FIELDS
#                             and field not in IGNORE_ITEM_FIELDS
#                         ):
#                             item_row[field] = value

#                     # Remove invalid BOM links
#                     if item_row.get("bom_no") and not frappe.db.exists("BOM", item_row["bom_no"]):
#                         item_row["bom_no"] = None

#                     wo.append("required_items", item_row)

#             # ------------------------------------------------
#             # OPERATIONS
#             # ------------------------------------------------
#             if hasattr(ext_wo, "operations"):

#                 wo.set("operations", [])

#                 for row in ext_wo.operations:

#                     op_row = {}

#                     for field, value in row.as_dict().items():

#                         if field not in SYSTEM_FIELDS:
#                             op_row[field] = value

#                     # Remove invalid Workstation links
#                     if op_row.get("workstation") and not frappe.db.exists("Workstation", op_row["workstation"]):
#                         op_row["workstation"] = None

#                     # Remove invalid Workstation Type links
#                     if op_row.get("workstation_type") and not frappe.db.exists("Workstation Type", op_row["workstation_type"]):
#                         op_row["workstation_type"] = None

#                     # Remove invalid BOM links
#                     if op_row.get("bom") and not frappe.db.exists("BOM", op_row["bom"]):
#                         op_row["bom"] = None

#                     wo.append("operations", op_row)

#             # ------------------------------------------------
#             # SCRAP ITEMS — FIXED: Only append if table exists
#             # ------------------------------------------------
#             # NOTE: Standard ERPNext does NOT have a separate scrap_items
#             # child table on Work Order. Scrap is handled in BOM or
#             # via required_items. Only process if the doctype actually exists.
#             if hasattr(ext_wo, "scrap_items") and frappe.db.exists("DocType", "Work Order Scrap Item"):
#                 wo.set("scrap_items", [])

#                 for row in ext_wo.scrap_items:

#                     scrap_row = {}

#                     for field, value in row.as_dict().items():

#                         if field not in SYSTEM_FIELDS:
#                             scrap_row[field] = value

#                     wo.append("scrap_items", scrap_row)

#             # ------------------------------------------------
#             # FLAGS
#             # ------------------------------------------------
#             wo.flags.ignore_permissions = True
#             wo.flags.ignore_validate = True
#             wo.flags.ignore_mandatory = True
#             wo.flags.ignore_links = True
#             wo.flags.ignore_naming_series = True

#             # ------------------------------------------------
#             # INSERT
#             # ------------------------------------------------
#             # target_name is now already set as wo.name above, so this
#             # inserts directly under the correct name — no rename, no
#             # raw-SQL re-parenting of child tables required.
#             wo.insert(
#                 ignore_permissions=True,
#                 ignore_links=True,
#                 ignore_mandatory=True
#             )

#             # ------------------------------------------------
#             # SAFETY NET: if Frappe still renamed it for any reason
#             # (e.g. a hook elsewhere forces autonaming), re-parent
#             # child tables defensively. This should normally be a
#             # no-op now, but is kept as a backstop. Note the original
#             # bug here: child_table.replace(" ", "") stripped the space
#             # out of "Work Order Item" -> "WorkOrderItem", which is NOT
#             # a real DocType name, so frappe.db.exists() always returned
#             # False and the UPDATE never ran. Fixed by checking the
#             # DocType name as-is (with its space).
#             # ------------------------------------------------
#             if wo.name != target_name:

#                 if not frappe.db.exists("Work Order", target_name):
#                     frappe.db.sql("""
#                         UPDATE `tabWork Order`
#                         SET name = %s
#                         WHERE name = %s
#                     """, (target_name, wo.name))

#                 old_name = wo.name

#                 child_tables = [
#                     "Work Order Item",       # required_items
#                     "Work Order Operation",  # operations
#                 ]

#                 if frappe.db.exists("DocType", "Work Order Scrap Item"):
#                     child_tables.append("Work Order Scrap Item")

#                 for child_table in child_tables:
#                     if frappe.db.exists("DocType", child_table):  # FIX: no space-stripping
#                         frappe.db.sql("""
#                             UPDATE `tab{0}`
#                             SET parent = %s
#                             WHERE parent = %s
#                         """.format(child_table), (target_name, old_name))

#                 # Update in-memory doc name to match DB
#                 wo.name = target_name

#             frappe.db.commit()

#             # ------------------------------------------------
#             # VERIFY: confirm required_items actually landed under
#             # the final name before moving on to submit/cancel logic.
#             # ------------------------------------------------
#             actual_item_count = frappe.db.count("Work Order Item", {"parent": target_name})
#             expected_item_count = len(ext_wo.get("required_items") or [])

#             if actual_item_count != expected_item_count:
#                 frappe.log_error(
#                     f"Work Order {target_name}: expected {expected_item_count} "
#                     f"required_items, found {actual_item_count} after insert.",
#                     "ExternalWorkOrder Sync - required_items mismatch"
#                 )

#             # ------------------------------------------------
#             # DOCSTATUS SYNC — FIXED: Map Status to docstatus
#             # ------------------------------------------------
#             # CRITICAL FIX: The CSV has 'Status' not 'docstatus'.
#             # Map workflow Status to document docstatus for submit/cancel logic.
#             wo_status = ext_wo.get("status") or "Draft"

#             # Map External Work Order Status to target docstatus
#             if wo_status in ("Completed", "In Process", "Not Started"):
#                 target_docstatus = 1  # Submit these
#             elif wo_status == "Cancelled":
#                 target_docstatus = 2  # Cancel
#             else:
#                 target_docstatus = 0  # Draft

#             # Always get a completely fresh document from DB
#             wo = frappe.get_doc("Work Order", target_name)

#             # Re-apply all flags on fresh document
#             wo.flags.ignore_permissions = True
#             wo.flags.ignore_validate = True
#             wo.flags.ignore_mandatory = True
#             wo.flags.ignore_links = True

#             # ------------------------------------------------
#             # SUBMIT / CANCEL based on mapped docstatus
#             # ------------------------------------------------
#             try:
#                 if target_docstatus == 1:
#                     wo.submit()

#                 elif target_docstatus == 2:
#                     wo.submit()

#                     # Get fresh doc after submit, apply ALL flags, then cancel
#                     wo = frappe.get_doc("Work Order", target_name)
#                     wo.flags.ignore_permissions = True
#                     wo.flags.ignore_validate = True
#                     wo.flags.ignore_mandatory = True
#                     wo.flags.ignore_links = True
#                     wo.cancel()

#             except Exception as submit_err:
#                 frappe.log_error(
#                     f"Work Order Submit/Cancel Error for {target_name}: {str(submit_err)}",
#                     "ExternalWorkOrder Sync - Submit/Cancel"
#                 )
#                 results.append({
#                     "name": target_name,
#                     "status": "synced_draft",
#                     "error": f"Inserted but submit/cancel failed: {str(submit_err)}"
#                 })
#                 continue

#             results.append({
#                 "name": target_name,
#                 "status": "synced",
#                 "required_items_count": actual_item_count
#             })

#         except Exception:

#             frappe.log_error(
#                 f"Work Order Sync Error: {name}",
#                 frappe.get_traceback()
#             )

#             results.append({
#                 "name": name,
#                 "status": "failed"
#             })

#     return results

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
        return round(flt(val), 6)

    return (
        str(item_row.get("item_code") or "").strip(),
        str(item_row.get("bom_no") or "").strip(),
        safe_float(item_row.get("qty")),
        str(item_row.get("sales_order") or "").strip(),
    )


def _make_operation_fingerprint(op_row):
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(op_row.get("operation") or "").strip(),
        str(op_row.get("workstation") or "").strip(),
        safe_float(op_row.get("time_in_mins")),
    )


def _make_scrap_item_fingerprint(scrap_row):
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(scrap_row.get("item_code") or "").strip(),
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


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_work_order(external_name, company):
    external = frappe.get_doc("External Work Order", external_name)

    company_abbr = _get_company_abbr(company)
    base_id = external.remote_id or external.name
    target_name = _build_target_name(company_abbr, base_id)

    # Check if it already exists locally
    if frappe.db.exists("Work Order", target_name):
        _write_back_external_link(external_name, target_name)
        return {"name": target_name, "status": "exists"}

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

    try:
        wo.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
    except Exception:
        frappe.log_error(title=f"Work Order Insert FAILED: {external_name}", message=frappe.get_traceback())
        raise

    _write_back_external_link(external_name, target_name)

    if _doctype_has_field("Work Order", "remote_id"):
        frappe.db.set_value("Work Order", target_name, "remote_id", base_id, update_modified=False)

    # ------------------------------------------------
    # DOCSTATUS SYNC — Map Status to docstatus
    # ------------------------------------------------
    wo_status = external.get("status") or "Draft"
    external_docstatus = cint(external.docstatus)

    # Determine target docstatus
    if external_docstatus == 1:
        target_docstatus = 1
    elif external_docstatus == 2:
        target_docstatus = 2
    elif wo_status in ("Completed", "In Process", "Not Started"):
        target_docstatus = 1
    elif wo_status == "Cancelled":
        target_docstatus = 2
    else:
        target_docstatus = 0

    # Always get a completely fresh document from DB
    wo = frappe.get_doc("Work Order", target_name)
    wo.flags.ignore_permissions = True
    wo.flags.ignore_validate = True
    wo.flags.ignore_mandatory = True
    wo.flags.ignore_links = True

    try:
        if target_docstatus == 1:
            wo.submit()

        elif target_docstatus == 2:
            if wo.docstatus == 0:
                wo.submit()
                
            if wo.docstatus == 1:
                wo = frappe.get_doc("Work Order", target_name)
                wo.flags.ignore_permissions = True
                wo.flags.ignore_validate = True
                wo.flags.ignore_mandatory = True
                wo.flags.ignore_links = True
                wo.cancel()

    except Exception as submit_err:
        frappe.log_error(
            title=f"Work Order Submit/Cancel Error: {target_name}",
            message=str(submit_err)
        )
        return {
            "name": target_name,
            "status": "synced_draft",
            "error": f"Inserted but submit/cancel failed: {str(submit_err)}"
        }

    actual_item_count = frappe.db.count("Work Order Item", {"parent": target_name})

    return {
        "name": target_name,
        "status": "synced",
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
