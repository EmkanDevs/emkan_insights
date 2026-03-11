# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import requests
import json
from urllib.parse import quote
from frappe import _


class ExternalSiteConfiguration(Document):
    pass


def load_doctypes_before_insert(doc, method=None):
    """Called via hooks before_insert — rows added to doc before it's saved"""
    fetch_rows = frappe.get_all(
        "Site Configuration Doctypes CT",
        fields=["ref_doctype", "exported_doctype"],
        order_by="idx asc",
        limit=200
    )

    if not fetch_rows:
        return

    existing_ref_doctypes = {row.ref_doctype for row in doc.fetch_data}

    for row in fetch_rows:
        if row.ref_doctype in existing_ref_doctypes:
            continue
        doc.append("fetch_data", {
            "ref_doctype": row.ref_doctype,
            "exported_doctype": row.exported_doctype
        })
        existing_ref_doctypes.add(row.ref_doctype)
    

@frappe.whitelist()
def get_site_config_doctypes_rows():
    """
    Returns all rows from Site Configuration Doctypes CT
    to populate External Site Configuration fetch_data child table.
    """
    rows = frappe.get_all(
        "Site Configuration Doctypes CT",
        fields=["ref_doctype", "exported_doctype"],
        order_by="idx asc",
        limit=200
    )
    return rows
# ─────────────────────────────────────────────────────────────────────────────
# HELPER: fetch full docs for doctypes that need child tables
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_remote_docs_with_children(base_url, headers, remote_dt, rows):
    docs = []
    encoded_dt = quote(str(remote_dt), safe='')
    for row in rows:
        name = row.get("name")
        if not name:
            docs.append(row)
            continue
        try:
            encoded_name = quote(str(name), safe='')
            doc_url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
            response = requests.get(doc_url, headers=headers, timeout=30)
            response.raise_for_status()
            doc = response.json().get("data") or row
            docs.append(doc)
        except Exception as e:
            frappe.logger("external_sync").warning(
                "Failed to fetch full doc for %s/%s: %s. Falling back to list payload.",
                remote_dt, name, str(e),
            )
            docs.append(row)
    return docs


# ─────────────────────────────────────────────────────────────────────────────
# SORTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _sort_accounts_by_reference_order(data):
    """
    Strict two-pass ordering:

    PASS 1 — ALL is_group=1 accounts sorted by depth (shallowest first).
             Guarantees every group parent exists before its child group,
             no matter how deep the hierarchy goes.

    PASS 2 — ALL is_group=0 (leaf) accounts sorted by depth.
             By this point every possible parent group is already saved,
             so no leaf can ever have a missing parent.

    Within each pass, ties broken by name for consistency.
    """
    id_map = {d.get('name'): d for d in data if d.get('name')}
    depths = {}

    def get_depth(account_id, visited=None):
        if account_id in depths:
            return depths[account_id]
        if visited is None:
            visited = set()
        if account_id in visited:
            depths[account_id] = 0
            return 0
        visited.add(account_id)
        item = id_map.get(account_id)
        if not item:
            depths[account_id] = 0
            return 0
        parent_id = item.get('parent_account')
        if not parent_id:
            depths[account_id] = 0
            return 0
        depth = get_depth(parent_id, visited) + 1
        depths[account_id] = depth
        return depth

    for item in data:
        if item.get('name'):
            get_depth(item['name'])

    groups = [d for d in data if frappe.utils.cint(d.get("is_group")) == 1]
    leaves = [d for d in data if frappe.utils.cint(d.get("is_group")) != 1]

    groups_sorted = sorted(groups, key=lambda x: (depths.get(x.get('name'), 0), x.get('name') or ""))
    leaves_sorted = sorted(leaves, key=lambda x: (depths.get(x.get('name'), 0), x.get('name') or ""))

    return groups_sorted + leaves_sorted


def _sort_by_hierarchy(data, parent_field):
    id_map = {d.get('name'): d for d in data if d.get('name')}
    depths = {}

    def get_depth(item_id, visited=None):
        if item_id in depths:
            return depths[item_id]
        if visited is None:
            visited = set()
        if item_id in visited:
            return 0
        visited.add(item_id)
        item = id_map.get(item_id)
        if not item:
            depths[item_id] = 0
            return 0
        parent_id = item.get(parent_field)
        if not parent_id or parent_id not in id_map:
            depths[item_id] = 0
            return 0
        d = get_depth(parent_id, visited) + 1
        depths[item_id] = d
        return d

    for item in data:
        if item.get('name'):
            get_depth(item['name'])

    return sorted(data, key=lambda x: depths.get(x.get('name'), 0))


# ─────────────────────────────────────────────────────────────────────────────
# TREE / PARENT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_meta_fieldnames(meta):
    return {df.fieldname for df in meta.fields}


def _ensure_tree_parent(local_dt, parent_field, name_field, parent_remote, company=None):
    if not parent_remote:
        return None

    parent_name = frappe.db.get_value(local_dt, {"remote_id": parent_remote}, "name")
    if not parent_name:
        parent_name = frappe.db.get_value(local_dt, {"name": parent_remote}, "name")
    if parent_name:
        return parent_name

    frappe.logger("external_sync").warning(
        "[STUB] Creating stub parent %s/%s — will be populated when its own row syncs.",
        local_dt, parent_remote,
    )
    parent_doc = frappe.new_doc(local_dt)
    parent_doc.name = parent_remote
    parent_doc.remote_id = parent_remote
    if name_field:
        parent_doc.set(name_field, parent_remote)
    if company and 'company' in _get_meta_fieldnames(parent_doc.meta):
        parent_doc.company = company
    if 'is_group' in _get_meta_fieldnames(parent_doc.meta):
        parent_doc.is_group = 1

    parent_doc.flags.ignore_links = True
    parent_doc.flags.ignore_permissions = True
    parent_doc.flags.ignore_mandatory = True
    try:
        parent_doc.insert()
    except Exception as e:
        if _is_duplicate_conflict(e):
            existing = frappe.db.get_value(local_dt, {"name": parent_remote}, "name")
            return existing or parent_remote
        raise
    frappe.db.commit()
    return parent_doc.name


def _ensure_root_account(local_dt, name_field, root_label, company=None):
    existing = frappe.db.get_value(local_dt, {"name": root_label}, "name")
    if existing:
        return existing

    root_doc = frappe.new_doc(local_dt)
    root_doc.name = root_label
    root_doc.remote_id = root_label
    if name_field:
        root_doc.set(name_field, root_label)
    if company and 'company' in _get_meta_fieldnames(root_doc.meta):
        root_doc.company = company
    if 'is_group' in _get_meta_fieldnames(root_doc.meta):
        root_doc.is_group = 1

    root_doc.flags.ignore_links = True
    root_doc.flags.ignore_permissions = True
    root_doc.flags.ignore_mandatory = True
    try:
        root_doc.insert()
    except Exception as e:
        if _is_duplicate_conflict(e):
            existing = frappe.db.get_value(local_dt, {"name": root_label}, "name")
            return existing or root_label
        raise
    frappe.db.commit()
    return root_doc.name


# ─────────────────────────────────────────────────────────────────────────────
# SYNC HELPERS (per-doctype post-processing)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_sync_helpers(local_dt, doc, item, local_fields, company):
    if local_dt == "External Account":
        if doc.is_new():
            incoming_name = item.get('name') or item.get('account_name')
            if incoming_name:
                doc.name = incoming_name
        if company and 'company' in local_fields and not doc.get('company'):
            doc.company = company

        parent_remote = item.get('parent_account')
        parent_name = None

        if parent_remote:
            parent_name = _ensure_tree_parent(
                local_dt, "parent_account", "account_name", parent_remote, company
            )

        if not parent_name:
            root_type = item.get('root_type') or "Root"
            root_label = f"External {root_type} Root"
            if company:
                root_label = f"{root_label} - {company}"
            parent_name = _ensure_root_account(local_dt, "account_name", root_label, company)

        if not parent_name:
            parent_name = _ensure_root_account(
                local_dt, "account_name", "External Account Root", company
            )

        if parent_name:
            doc.parent_account = parent_name
            
    if local_dt == "External Address":
    # Auto-fill address_title if missing
        if not doc.get("address_title"):
            doc.address_title = (
                item.get("address_title")
                or item.get("address_line1")
                or item.get("name")
            )
        
    if local_dt == "External Cost Center":
        if doc.is_new() and item.get('name'):
            doc.name = item.get('name')
        if company and 'company' in local_fields and not doc.get('company'):
            doc.company = company
        parent_remote = item.get('parent_cost_center')
        parent_name = _ensure_tree_parent(
            local_dt, "parent_cost_center", "cost_center_name", parent_remote, company
        )
        if parent_name:
            doc.parent_cost_center = parent_name

    if local_dt == "External Asset Category":
        if 'accounts' in item and 'accounts' in local_fields:
            doc.set('accounts', item.get('accounts') or [])

    if local_dt == "External Asset":
        if company and 'company' in local_fields and not doc.get('company'):
            doc.company = company

    if local_dt == "External Material Request":
        # Ensure every item row has a warehouse
        for row in doc.get("items"):
            if not row.warehouse:
                # Use a default warehouse or fetch from a local setting
                row.warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")


# ─────────────────────────────────────────────────────────────────────────────
# CORE UPSERT
# NOTE: The duplicate account_name guard has been intentionally removed.
#       In ERPNext, account_name is NOT unique — only the full doc name is
#       (e.g. "Owner Equity - IMC").  Multiple accounts can share the same
#       account_name (e.g. a group "Owner Equity" and a child "Owner Equity").
#       The old guard was wrongly blocking these legitimate accounts.
# ─────────────────────────────────────────────────────────────────────────────

# Return values for _process_item:
#   True     — record was saved/updated successfully this run
#   "exists" — record already exists in DB and was skipped (not an error)
#   False    — genuine failure, record is missing from DB

def _process_item(local_dt, item, idx, local_fields, company, configuration_name, errors):
    remote_id = item.get('name') or item.get('id')
    remote_docstatus = frappe.utils.cint(item.get("docstatus", 0))
    
    if not remote_id:
        return False

    existing_name = frappe.db.get_value(local_dt, {"remote_id": remote_id}, "name")
    
    if existing_name:
        doc = frappe.get_doc(local_dt, existing_name)
        # --- FIX: CLEAR CHILD TABLES BEFORE MAPPING NEW DATA ---
        # This prevents the "Not Found" error by removing local references 
        # to rows that no longer exist or have changed names.
        for df in doc.meta.get_table_fields():
            doc.set(df.fieldname, [])
    else:
        doc = frappe.new_doc(local_dt)
        doc.remote_id = remote_id
        doc.source_site = configuration_name

    # Map fields
    for field, value in item.items():
        if field in local_fields and field not in ['name', 'owner', 'creation', 'modified', 'naming_series', 'docstatus']:
            if value is not None:
                # Clean child rows: Strip remote 'name' to avoid ID conflicts
                if isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict):
                            row.pop("name", None)
                            row.pop("parent", None)
                            row.pop("parenttype", None)
                            row.pop("parentfield", None)
                doc.set(field, value)

    # Standard Flags
    doc.flags.ignore_links = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_validate = True

    # Apply specialized helpers (like warehouse logic)
    _apply_sync_helpers(local_dt, doc, item, local_fields, company)

    try:
        doc.save()
        
        # Sync docstatus using db_set to bypass local submission rules
        if doc.docstatus != remote_docstatus:
            frappe.db.set_value(doc.doctype, doc.name, "docstatus", remote_docstatus, update_modified=False)
        
        frappe.db.commit()
        return True

    except Exception as e:
        # This will now capture any remaining validation or database errors
        errors.append({"remote_id": remote_id, "error": str(e)})
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SHARED SYNC LOOP
# Tracks saved vs not-saved purely from loop results — no helper logging needed.
# ─────────────────────────────────────────────────────────────────────────────
def _run_sync_loop(data, local_dt, local_fields, company, configuration_name):
    """
    Return values from _process_item:
      True     → saved/updated this run        → count++
      "exists" → already in DB, skipped        → count++
      False    → genuine failure               → added to not_saved report
    """
    count = 0
    errors = []
    not_saved = []

    for idx, item in enumerate(data, start=1):
        result = _process_item(local_dt, item, idx, local_fields, company, configuration_name, errors)
        if result:
            count += 1
        else:
            not_saved.append(item) # Add more detail as needed
            
    return count, errors, not_saved


# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNT SYNC (dedicated endpoint)
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def sync_accounts_from_remote(site_url, api_key, api_secret, child_docname=None, company=None, configuration_name=None):
    frappe.logger("external_sync").info("Starting account sync with company %s", company)

    base_url = (site_url or "").rstrip('/')
    headers = {
        'Authorization': f'token {api_key}:{api_secret}',
        'Content-Type': 'application/json'
    }

    remote_url = f"{base_url}/api/resource/Account"
    data = []
    limit = 1000
    start = 0

    try:
        while True:
            params = {
                "fields": json.dumps(["*"]),
                "limit_page_length": limit,
                "limit_start": start
            }
            response = requests.get(remote_url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            page = response.json().get('data', [])
            if not page:
                break
            data.extend(page)
            if len(page) < limit:
                break
            start += limit
    except Exception as e:
        frappe.throw(_("Account sync failed: {0}").format(str(e)))

    local_dt = "External Account"
    local_meta = frappe.get_meta(local_dt)
    local_fields = [df.fieldname for df in local_meta.fields]
    data = _sort_accounts_by_reference_order(data)

    count, errors, not_saved = _run_sync_loop(
        data, local_dt, local_fields, company, configuration_name
    )

    last_sync = frappe.utils.now()
    if child_docname:
        frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
    frappe.db.commit()

    return {
        "count": count,
        "fetched_total": len(data),
        "not_saved_count": len(not_saved),
        "last_sync": last_sync,
        "errors": errors,
        "missing_parent_accounts": not_saved,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERIC SYNC ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def sync_data_from_remote(site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None):
    frappe.logger("external_sync").info("Starting sync for %s with company %s", ref_doctype, company)

    base_url = (site_url or "").rstrip('/')
    headers = {
        'Authorization': f'token {api_key}:{api_secret}',
        'Content-Type': 'application/json'
    }

    doctype_map = {
        "Account": "External Account",
        "Address": "External Address",
        "Asset": "External Asset",
        "Asset Category": "External Asset Category",
        "Bank": "External Bank",
        "Bank Account": "External Bank Account",
        "Contact": "External Contact",
        "Contract": "External Contract",
        "Cost Center": "External Cost Center",
        "Customer": "External Customer",
        "Item": "External Item",
        "Item Group": "External Item Group",
        "Location": "External Location",
        "Manufacturer": "External Manufacturer",
        "Price List": "External Price List",
        "Project Type": "External Project Type",
        "Supplier": "External Supplier",
        "Supplier Group": "External Supplier Group",
        "Territory": "External Territory",
        "UOM": "External Uom",
        "Warehouse": "External Warehouse",
        "Purchase Invoice": "External Purchase Invoice",
        "Payment Entry": "External Payment Entry",
        "Purchase Order" : "External Purchase Order",
        "Sales Order": "External Sales Order",
        "Stock Entry": "External Stock Entry",
        "Project" : "External Project",
        "Request for Quotation": "External Request for Quotation",
        "Supplier Quotation": "External Supplier Quotation",
        "Purchase Receipt": "External Purchase Receipt",
        "Quotation": "External Quotation",
        "Delivery Note": "External Delivery Note",
        "Sales Invoice": "External Sales Invoice",
        "Sales Taxes and Charges Template": "External Sales Taxes and Charges Template",
        "Purchase Taxes and Charges Template": "External Purchase Taxes and Charges Template",
        "Letter Head": "External Letter Head",
        "Expense Claim": "External Expense Claim",
        "Payment Terms Template": "External Payment Terms Template",
        "Sales Person": "External Sales Person",
        "Terms and Conditions": "External Terms and Conditions",
        "Journal Entry": "External Journal Entry",
        "Material Request" : "External Material Request"
    }
    reverse_doctype_map = {local: remote for remote, local in doctype_map.items()}
    remote_dt = reverse_doctype_map.get(ref_doctype, ref_doctype)
    local_dt = doctype_map.get(remote_dt)

    if not local_dt:
        frappe.log_error(title="Sync Data Error", message=f"Doctype mapping not found for: {ref_doctype}")
        frappe.throw(_("Mapping for {0} not found").format(ref_doctype))

    if not frappe.db.exists("DocType", local_dt):
        frappe.throw(
            _("Local DocType {0} is not installed on this site. Run bench migrate and reload.")
            .format(local_dt)
        )

    remote_url = f"{base_url}/api/resource/{remote_dt}"
    data = []
    limit = 1000
    start = 0
    try:
        while True:
            params = {
                "fields": json.dumps(["*"]),
                "limit_page_length": limit,
                "limit_start": start
            }
            response = requests.get(remote_url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            page = response.json().get('data', [])
            if not page:
                break
            data.extend(page)
            if len(page) < limit:
                break
            start += limit
    except Exception as e:
        frappe.throw(_("Sync failed: {0}").format(str(e)))

    if remote_dt in ["Asset Category", "Purchase Invoice", "Payment Entry" , "Purchase Order","Stock Entry","Purchase Receipt","Request for Quotation","Supplier Quotation","Quotation","Delivery Note","Sales Invoice","Sales Taxes and Charges Template","Purchase Taxes and Charges Template","Letter Head","Expense Claim","Payment Terms Template","Sales Person","Terms and Conditions","Sales Order","Material Request"] and data:
        data = _fetch_remote_docs_with_children(base_url, headers, remote_dt, data)

    local_meta = frappe.get_meta(local_dt)
    local_fields = [df.fieldname for df in local_meta.fields]

    if local_dt == "External Account":
        data = _sort_accounts_by_reference_order(data)
    elif local_dt == "External Cost Center":
        data = _sort_by_hierarchy(data, "parent_cost_center")
    elif local_dt == "External Item Group":
        data = _sort_by_hierarchy(data, "parent_item_group")

    count, errors, not_saved = _run_sync_loop(
        data, local_dt, local_fields, company, configuration_name
    )

    last_sync = frappe.utils.now()
    frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)
    frappe.db.commit()

    return {
        "count": count,
        "fetched_total": len(data),
        "not_saved_count": len(not_saved),
        "last_sync": last_sync,
        "errors": errors,
        "missing_parent_accounts": not_saved,
    }


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def _is_duplicate_conflict(error: Exception) -> bool:
    message = (str(error) or "").lower()
    return (
        "already exists" in message
        or "must be unique" in message
        or "duplicate entry" in message
    )


def _clean_child_rows(doc):
    for table_field in doc.meta.get_table_fields():
        rows = doc.get(table_field.fieldname) or []
        cleaned = []
        for row in rows:
            row = row.as_dict()

            # Remove remote linkage fields
            row.pop("name", None)
            row.pop("parent", None)
            row.pop("parenttype", None)
            row.pop("parentfield", None)

            cleaned.append(row)

        doc.set(table_field.fieldname, cleaned)