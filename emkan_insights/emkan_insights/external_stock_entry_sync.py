# import frappe
# import json
# from frappe.utils import cint

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }


# @frappe.whitelist()
# def sync_stock_entry_docs(source_doctype=None, names=None):
#     """
#     Handles bulk sync from List View.
#     """
#     # Use print for guaranteed console output
#     print("\n" + "="*60)
#     print("SYNC STOCK ENTRY DOCS CALLED")
#     print(f"source_doctype={source_doctype}")
#     print(f"names type={type(names)}")
#     print(f"names={names}")
#     print("="*60)

#     if isinstance(names, str):
#         names = json.loads(names)

#     print(f"Parsed names count={len(names) if names else 0}")

#     if not names:
#         print("NO NAMES PROVIDED - returning empty")
#         return []

#     results = []

#     for name in names:
#         try:
#             print(f"\n--- Processing name={name} ---")
#             external_doc = frappe.get_doc("External Stock Entry", name)
#             print(f"Fetched external_doc: {external_doc.name}, company={external_doc.company}")
#             result = sync_external_stock_entry(name, external_doc.company)
#             print(f"Result: {result}")
#             results.append(result)
#         except Exception as e:
#             error_msg = frappe.get_traceback()
#             # CORRECT: title first, message second
#             frappe.log_error(
#                 title=f"Stock Entry Sync Error: {name}",
#                 message=error_msg
#             )
#             print(f"ERROR for {name}: {str(e)}")
#             results.append({
#                 "name": name,
#                 "status": "failed",
#                 "error": str(e)
#             })

#     print(f"\nFinal results count: {len(results)}")
#     print(f"Results: {results}")
#     return results


# def sync_external_stock_entry(external_name, company):
#     """
#     Sync a single External Stock Entry to Stock Entry.
#     """
#     print(f"\n>>> sync_external_stock_entry called for {external_name}")

#     external = frappe.get_doc("External Stock Entry", external_name)
#     target_name = external.remote_id or external.name

#     print(f"target_name={target_name}")
#     print(f"external.remote_id={external.remote_id}")
#     print(f"external.name={external.name}")

#     # 1. Check if already exists
#     exists = frappe.db.exists("Stock Entry", target_name)
#     print(f"frappe.db.exists('Stock Entry', '{target_name}') = {exists}")

#     if exists:
#         print(f"Already exists! Returning 'exists'")
#         frappe.db.set_value("External Stock Entry", external_name, "remote_id", target_name)
#         return {
#             "name": target_name,
#             "status": "exists"
#         }

#     # 2. Create new Stock Entry
#     print("Creating new Stock Entry...")
#     se = frappe.new_doc("Stock Entry")
#     se.remote_id = external.remote_id or external.name

#     # Copy main fields
#     for field, value in external.as_dict().items():
#         if (
#             field not in SYSTEM_FIELDS
#             and field not in ["items", "remote_id", "additional_costs"]
#             and hasattr(se, field)
#         ):
#             se.set(field, value)

#     se.company = company
#     print(f"Company set to: {company}")

#     # Items
#     if hasattr(external, "items"):
#         item_count = len(external.items) if external.items else 0
#         print(f"External has {item_count} items")
#         se.set("items", [])

#         for idx, row in enumerate(external.items):
#             item_row = {}
#             for field, value in row.as_dict().items():
#                 if field not in SYSTEM_FIELDS:
#                     item_row[field] = value

#             # Map External Item to local Item
#             if item_row.get("item_code"):
#                 mapped_item = frappe.db.get_value("External Item", item_row["item_code"], "remote_id")
#                 print(f"  Item {idx}: {item_row['item_code']} -> mapped={mapped_item}")
#                 if mapped_item:
#                     item_row["item_code"] = mapped_item

#             se.append("items", item_row)

#     else:
#         print("WARNING: External has NO items attribute!")

#     # Flags
#     se.flags.ignore_permissions = True
#     se.flags.ignore_validate = True
#     se.flags.ignore_mandatory = True
#     se.flags.ignore_links = True

#     print(f"About to insert. se.name before insert = {se.name}")

#     # INSERT
#     try:
#         se.insert(
#             ignore_permissions=True,
#             ignore_links=True,
#             ignore_mandatory=True
#         )
#         print(f"Insert SUCCESS! se.name after insert = {se.name}")
#     except Exception as e:
#         print(f"Insert FAILED: {str(e)}")
#         frappe.log_error(
#             title=f"Stock Entry Insert FAILED: {external_name}",
#             message=frappe.get_traceback()
#         )
#         raise

#     # Rename if needed
#     if se.name != target_name:
#         print(f"Renaming {se.name} -> {target_name}")
#         frappe.db.sql("""
#             UPDATE `tabStock Entry`
#             SET name = %s
#             WHERE name = %s
#         """, (target_name, se.name))

#         # Rename child table
#         frappe.db.sql("""
#             UPDATE `tabStock Entry Detail`
#             SET parent = %s
#             WHERE parent = %s
#         """, (target_name, se.name))

#         se.name = target_name
#     else:
#         print(f"No rename needed")

#     # Map back
#     frappe.db.set_value("External Stock Entry", external_name, "remote_id", target_name)
#     if frappe.get_meta("Stock Entry").has_field("remote_id"):
#         frappe.db.set_value("Stock Entry", target_name, "remote_id", external.remote_id)

#     print(f"Set remote_id mapping complete")

#     # Submit if needed
#     if cint(external.docstatus) == 1:
#         print(f"Submitting (docstatus=1)")
#         se = frappe.get_doc("Stock Entry", target_name)
#         se.submit()

#     print(f"<<< DONE, returning {target_name}")
#     return {
#         "name": target_name,
#         "status": "synced"
#     }



# import frappe
# import json
# from frappe.utils import cint

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }


# @frappe.whitelist()
# def sync_stock_entry_docs(source_doctype=None, names=None):
#     """
#     Handles bulk sync from List View.
#     """
#     print("\n" + "="*60)
#     print("SYNC STOCK ENTRY DOCS CALLED")
#     print(f"source_doctype={source_doctype}")
#     print(f"names type={type(names)}")
#     print(f"names={names}")
#     print("="*60)

#     if isinstance(names, str):
#         names = json.loads(names)

#     print(f"Parsed names count={len(names) if names else 0}")

#     if not names:
#         print("NO NAMES PROVIDED - returning empty")
#         return []

#     results = []

#     for name in names:
#         try:
#             print(f"\n--- Processing name={name} ---")
#             external_doc = frappe.get_doc("External Stock Entry", name)
#             print(f"Fetched external_doc: {external_doc.name}, company={external_doc.company}")
#             result = sync_external_stock_entry(name, external_doc.company)
#             print(f"Result: {result}")
#             results.append(result)
#         except Exception as e:
#             error_msg = frappe.get_traceback()
#             frappe.log_error(
#                 title=f"Stock Entry Sync Error: {name}",
#                 message=error_msg
#             )
#             print(f"ERROR for {name}: {str(e)}")
#             results.append({
#                 "name": name,
#                 "status": "failed",
#                 "error": str(e)
#             })

#     print(f"\nFinal results count: {len(results)}")
#     print(f"Results: {results}")
#     return results


# def sync_external_stock_entry(external_name, company):
#     """
#     Sync a single External Stock Entry to Stock Entry.
#     """
#     print(f"\n>>> sync_external_stock_entry called for {external_name}")

#     external = frappe.get_doc("External Stock Entry", external_name)

#     company_abbr = frappe.db.get_value("Company", company, "abbr")
#     if not company_abbr:
#         frappe.throw(f"Company '{company}' has no abbreviation set")

#     base_id = external.remote_id or external.name
#     target_name = f"{company_abbr}-{base_id}"

#     print(f"company_abbr={company_abbr}")
#     print(f"base_id={base_id}")
#     print(f"target_name={target_name}")
#     print(f"external.remote_id={external.remote_id}")
#     print(f"external.name={external.name}")

#     # 1. Check if already exists
#     exists = frappe.db.exists("Stock Entry", target_name)
#     print(f"frappe.db.exists('Stock Entry', '{target_name}') = {exists}")

#     if exists:
#         print(f"Already exists! Returning 'exists'")
#         frappe.db.set_value("External Stock Entry", external_name, "remote_id", target_name)
#         return {
#             "name": target_name,
#             "status": "exists"
#         }

#     # 2. Create new Stock Entry
#     print("Creating new Stock Entry...")
#     se = frappe.new_doc("Stock Entry")
#     se.remote_id = external.remote_id or external.name

#     # Copy main fields
#     for field, value in external.as_dict().items():
#         if (
#             field not in SYSTEM_FIELDS
#             and field not in ["items", "remote_id", "additional_costs"]
#             and hasattr(se, field)
#         ):
#             se.set(field, value)

#     se.company = company
#     print(f"Company set to: {company}")

#     # Items
#     if hasattr(external, "items"):
#         item_count = len(external.items) if external.items else 0
#         print(f"External has {item_count} items")
#         se.set("items", [])

#         for idx, row in enumerate(external.items):
#             item_row = {}
#             for field, value in row.as_dict().items():
#                 if field not in SYSTEM_FIELDS:
#                     item_row[field] = value

#             # Map External Item to local Item
#             if item_row.get("item_code"):
#                 mapped_item = frappe.db.get_value("External Item", item_row["item_code"], "remote_id")
#                 print(f"  Item {idx}: {item_row['item_code']} -> mapped={mapped_item}")
#                 if mapped_item:
#                     item_row["item_code"] = mapped_item

#             se.append("items", item_row)

#     else:
#         print("WARNING: External has NO items attribute!")

#     # Force target name before insert (avoids post-insert rename + child table UPDATE hack)
#     se.flags.name_set = True
#     se.name = target_name
#     print(f"Forcing name pre-insert: se.name = {se.name}")

#     # Flags
#     se.flags.ignore_permissions = True
#     se.flags.ignore_validate = True
#     se.flags.ignore_mandatory = True
#     se.flags.ignore_links = True

#     print(f"About to insert. se.name before insert = {se.name}")

#     # INSERT
#     try:
#         se.insert(
#             ignore_permissions=True,
#             ignore_links=True,
#             ignore_mandatory=True
#         )
#         print(f"Insert SUCCESS! se.name after insert = {se.name}")
#     except Exception as e:
#         print(f"Insert FAILED: {str(e)}")
#         frappe.log_error(
#             title=f"Stock Entry Insert FAILED: {external_name}",
#             message=frappe.get_traceback()
#         )
#         raise

#     # Map back
#     frappe.db.set_value("External Stock Entry", external_name, "remote_id", target_name)
#     if frappe.get_meta("Stock Entry").has_field("remote_id"):
#         frappe.db.set_value("Stock Entry", target_name, "remote_id", external.remote_id)

#     print(f"Set remote_id mapping complete")

#     # Submit if needed
#     if cint(external.docstatus) == 1:
#         print(f"Submitting (docstatus=1)")
#         se = frappe.get_doc("Stock Entry", target_name)
#         se.submit()

#     print(f"<<< DONE, returning {target_name}")
#     return {
#         "name": target_name,
#         "status": "synced"
#     }



import json
import frappe
from frappe.utils import cint, flt


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

# Parent-level links on Stock Entry
STOCK_ENTRY_PARENT_LINKS = {
    "work_order": "Work Order",
    "purchase_order": "Purchase Order",
    "job_card": "Job Card",
    "bom_no": "BOM",
    "material_request": "Material Request",
    "sales_order": "Sales Order",
}

# Child-level links on Stock Entry Detail
STOCK_ENTRY_ITEM_LINKS = {
    "work_order": "Work Order",
    "purchase_order": "Purchase Order",
    "job_card": "Job Card",
    "bom_no": "BOM",
    "material_request": "Material Request",
    "sales_order": "Sales Order",
}

# If one of these fields exists on External Stock Entry,
# local Stock Entry name will be written back there.
EXTERNAL_LINK_BACK_FIELDS = (
    "stock_entry",
    "stock_entry_name",
    "local_stock_entry",
    "synced_stock_entry",
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
        frappe.throw("Missing base id for Stock Entry naming")

    if base_id.startswith(f"{company_abbr}-"):
        return base_id

    return f"{company_abbr}-{base_id}"


def _doctype_has_field(doctype, fieldname):
    meta = frappe.get_meta(doctype)
    return meta.has_field(fieldname) or fieldname in meta.get_valid_columns()


def _filter_valid_fields(doctype, values):
    meta = frappe.get_meta(doctype)
    valid_fields = set(meta.get_valid_columns())
    return {k: v for k, v in values.items() if k in valid_fields}


def _resolve_link_with_abbr(doctype, remote_name, company_abbr=None):
    """
    Resolve external link to local record using:
    1. already-prefixed local name
    2. company_abbr + '-' + remote_name
    3. remote_id/custom_remote_id lookup
    4. exact name fallback
    """
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
    """
    Resolve external item code to local ERPNext Item code.
    Assumption:
    External Item.name == incoming external item_code
    External Item.remote_id == local Item code
    """
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


def _apply_parent_link_mapping(se, external, company_abbr):
    """
    Resolve parent-level links on Stock Entry.
    """
    for fieldname, linked_doctype in STOCK_ENTRY_PARENT_LINKS.items():
        if not _doctype_has_field("Stock Entry", fieldname):
            continue

        raw_value = getattr(external, fieldname, None)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            se.set(fieldname, resolved)
        else:
            frappe.log_error(
                title=f"Stock Entry parent link unresolved: {external.name}",
                message=(
                    f"Field: {fieldname}\n"
                    f"Linked Doctype: {linked_doctype}\n"
                    f"Incoming Value: {raw_value}\n"
                    f"Company Abbr: {company_abbr}"
                )
            )


def _apply_item_link_mapping(item_row, company_abbr, external_name=None):
    """
    Resolve child-row links on Stock Entry Detail.
    """
    for fieldname, linked_doctype in STOCK_ENTRY_ITEM_LINKS.items():
        raw_value = item_row.get(fieldname)
        if not raw_value:
            continue

        resolved = _resolve_link_with_abbr(linked_doctype, raw_value, company_abbr)
        if resolved:
            item_row[fieldname] = resolved
        else:
            frappe.log_error(
                title=f"Stock Entry item link unresolved: {external_name or 'Unknown'}",
                message=(
                    f"Field: {fieldname}\n"
                    f"Linked Doctype: {linked_doctype}\n"
                    f"Incoming Value: {raw_value}\n"
                    f"Company Abbr: {company_abbr}"
                )
            )


def _resolve_material_request_item(item_row):
    """
    If material_request was resolved locally, resolve material_request_item
    using local parent + item_code if possible.
    """
    mr_name = item_row.get("material_request")
    item_code = item_row.get("item_code")

    if not mr_name or not item_code:
        return

    mr_item_name = frappe.db.get_value(
        "Material Request Item",
        {
            "parent": mr_name,
            "item_code": item_code
        },
        "name"
    )

    if mr_item_name:
        item_row["material_request_item"] = mr_item_name


def _write_back_external_link(external_name, target_name):
    """
    Preserve External Stock Entry.remote_id.
    Write local Stock Entry name back only if a dedicated field exists.
    """
    meta = frappe.get_meta("External Stock Entry")

    for fieldname in EXTERNAL_LINK_BACK_FIELDS:
        if meta.has_field(fieldname):
            frappe.db.set_value(
                "External Stock Entry",
                external_name,
                fieldname,
                target_name,
                update_modified=False
            )
            return fieldname

    return None


def _make_item_fingerprint(item_row):
    """
    Fingerprint to prevent exact duplicate Stock Entry Detail rows.
    Uses rounded floats to handle precision mismatches.
    """
    # Use rounding to avoid float precision issues (e.g. 10.0 vs 10.000000001)
    def safe_float(val):
        return round(flt(val), 6)

    return (
        str(item_row.get("item_code") or "").strip(),
        str(item_row.get("s_warehouse") or "").strip(),
        str(item_row.get("t_warehouse") or "").strip(),
        safe_float(item_row.get("qty")),
        safe_float(item_row.get("basic_rate") or item_row.get("valuation_rate") or 0),
        str(item_row.get("uom") or "").strip(),
        str(item_row.get("batch_no") or "").strip(),
        str(item_row.get("serial_no") or "").strip(),
        str(item_row.get("work_order") or "").strip(),
        str(item_row.get("purchase_order") or "").strip(),
        str(item_row.get("job_card") or "").strip(),
        str(item_row.get("bom_no") or "").strip(),
        str(item_row.get("material_request") or "").strip(),
        str(item_row.get("sales_order") or "").strip(),
        safe_float(item_row.get("transfer_qty")),
        safe_float(item_row.get("conversion_factor")),
    )


def _append_stock_entry_items(se, external, company_abbr):
    """
    Build and append Stock Entry Detail rows.
    Also protects against duplicate child rows.
    """
    se.set("items", [])

    if not hasattr(external, "items") or not external.items:
        return

    seen = set()

    for row in external.items:
        item_row = {}

        for field, value in row.as_dict().items():
            if field in SYSTEM_FIELDS:
                continue
            item_row[field] = value

        # External Item -> local Item
        if item_row.get("item_code"):
            item_row["item_code"] = _resolve_item_code(item_row["item_code"])

        # Resolve requested linked doctypes
        _apply_item_link_mapping(item_row, company_abbr, external.name)

        # If MR got remapped, try to remap MR item too
        _resolve_material_request_item(item_row)

        # Keep only valid child fields
        item_row = _filter_valid_fields("Stock Entry Detail", item_row)

        # Dedup exact repeated rows
        fingerprint = _make_item_fingerprint(item_row)
        if fingerprint in seen:
            frappe.log_error(
                title=f"Duplicate Stock Entry item skipped: {external.name}",
                message=f"Duplicate row skipped for item_code={item_row.get('item_code')}"
            )
            continue

        seen.add(fingerprint)
        se.append("items", item_row)


# ==========================================================
# MAIN SYNC
# ==========================================================

def sync_external_stock_entry(external_name, company):
    """
    Sync a single External Stock Entry into Stock Entry.
    """
    external = frappe.get_doc("External Stock Entry", external_name)

    company_abbr = _get_company_abbr(company)
    base_id = external.remote_id or external.name
    target_name = _build_target_name(company_abbr, base_id)

    if frappe.db.exists("Stock Entry", target_name):
        _write_back_external_link(external_name, target_name)
        return {
            "name": target_name,
            "status": "exists"
        }

    se = frappe.new_doc("Stock Entry")

    # IMPORTANT: Disable BOM auto-pull immediately on creation
    if _doctype_has_field("Stock Entry", "from_bom"):
        se.from_bom = 0
    if _doctype_has_field("Stock Entry", "use_multi_level_bom"):
        se.use_multi_level_bom = 0

    if _doctype_has_field("Stock Entry", "remote_id"):
        se.remote_id = base_id

    # Copy header fields
    source_data = external.as_dict()
    for field, value in source_data.items():
        if field in SYSTEM_FIELDS:
            continue

        # Do not copy child tables or from_bom blindly
        if field in ("items", "additional_costs", "from_bom"):
            continue

        if _doctype_has_field("Stock Entry", field):
            se.set(field, value)

    se.company = company

    # Resolve parent links to local prefixed records
    _apply_parent_link_mapping(se, external, company_abbr)

    # Child items
    _append_stock_entry_items(se, external, company_abbr)

    # Force deterministic local name
    se.flags.name_set = True
    se.name = target_name

    se.flags.ignore_permissions = True
    se.flags.ignore_validate = True
    se.flags.ignore_mandatory = True
    se.flags.ignore_links = True

    try:
        se.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
    except Exception:
        frappe.log_error(
            title=f"Stock Entry Insert FAILED: {external_name}",
            message=frappe.get_traceback()
        )
        raise

    _write_back_external_link(external_name, target_name)

    if _doctype_has_field("Stock Entry", "remote_id"):
        frappe.db.set_value(
            "Stock Entry",
            target_name,
            "remote_id",
            base_id,
            update_modified=False
        )

    if cint(external.docstatus) == 1:
        submitted_doc = frappe.get_doc("Stock Entry", target_name)
        submitted_doc.flags.ignore_permissions = True
        submitted_doc.submit()

    return {
        "name": target_name,
        "status": "synced"
    }


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_stock_entry_docs(source_doctype=None, names=None):
    """
    Handles bulk sync from List View.
    """
    names = _parse_names(names)
    if not names:
        return []

    results = []

    for name in names:
        try:
            external_doc = frappe.get_doc("External Stock Entry", name)
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