# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import requests
import json
from urllib.parse import quote
from frappe import _
from urllib.parse import quote
from frappe.utils import get_datetime_str


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
# CONCURRENCY LOCK
# ─────────────────────────────────────────────────────────────────────────────
def _acquire_sync_lock(child_docname, timeout=1800):
    key = f"external_sync_lock:{child_docname}"
    try:
        acquired = frappe.cache().redis.set(key, "1", nx=True, ex=timeout)
        return bool(acquired)
    except Exception:
        frappe.logger("external_sync").warning(
            "Could not acquire redis lock for %s - proceeding without lock.",
            child_docname
        )
        return True


def _release_sync_lock(child_docname):
    key = f"external_sync_lock:{child_docname}"
    try:
        frappe.cache().redis.delete(key)
    except Exception:
        pass


def _locked_response(child_docname):
    return {
        "count": 0,
        "fetched_total": 0,
        "not_saved_count": 0,
        "last_sync": None,
        "errors": [{
            "remote_id": None,
            "error": f"A sync is already running for this row ({child_docname}). "
                     f"Please wait for it to finish before triggering it again."
        }],
        "missing_parent_accounts": [],
    }



# ─────────────────────────────────────────────────────────────────────────────
# HELPER: fetch full docs for doctypes that need child tables
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_remote_docs_with_children(base_url, headers, remote_dt, rows):
    docs = []
    encoded_dt = quote(str(remote_dt), safe='')
    seen_remote_names = set()

    for row in rows:
        name = row.get("name")
        if not name:
            docs.append(row)
            continue
        if name in seen_remote_names:
            continue
        seen_remote_names.add(name)
        try:
            encoded_name = quote(str(name), safe='')
            doc_url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
            response = requests.get(doc_url, headers=headers, timeout=30)
            response.raise_for_status()

            doc = response.json().get("data") or row

            # --- SCRUBBING LOGIC START ---
            # if remote_dt == "Quotation":
            #     if doc.get("cost_center") == problematic_cc:
            #         doc["cost_center"] = None

            #     for key, value in doc.items():
            #         if isinstance(value, list):
            #             for child_row in value:
            #                 if isinstance(child_row, dict) and child_row.get("cost_center") == problematic_cc:
            #                     child_row["cost_center"] = None
            # --- SCRUBBING LOGIC END ---

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


def _sort_warehouses_by_hierarchy(data):
    """
    Two-pass sort for Warehouses — mirrors _sort_accounts_by_reference_order.

    PASS 1 — ALL is_group=1 warehouses sorted by depth (shallowest first).
             Guarantees every group parent exists before any of its children.

    PASS 2 — ALL is_group=0 (leaf) warehouses sorted by depth.
             By this point every possible parent group is already saved,
             so no leaf can ever have a missing parent.

    Depth is calculated only within the current chunk; if a parent is NOT in
    this chunk it is treated as depth 0 (will be stub-created if missing).
    """
    id_map = {d.get('name'): d for d in data if d.get('name')}
    depths = {}

    def get_depth(item_id, visited=None):
        if item_id in depths:
            return depths[item_id]
        if visited is None:
            visited = set()
        if item_id in visited:
            depths[item_id] = 0
            return 0
        visited.add(item_id)
        item = id_map.get(item_id)
        if not item:
            depths[item_id] = 0
            return 0
        parent_id = item.get('parent_warehouse')
        if not parent_id:
            depths[item_id] = 0
            return 0
        depth = get_depth(parent_id, visited) + 1
        depths[item_id] = depth
        return depth

    for item in data:
        if item.get('name'):
            get_depth(item['name'])

    groups = [d for d in data if frappe.utils.cint(d.get("is_group")) == 1]
    leaves = [d for d in data if frappe.utils.cint(d.get("is_group")) != 1]

    groups_sorted = sorted(groups, key=lambda x: (depths.get(x.get('name'), 0), x.get('name') or ""))
    leaves_sorted = sorted(leaves, key=lambda x: (depths.get(x.get('name'), 0), x.get('name') or ""))

    return groups_sorted + leaves_sorted


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
    return root_doc.name


# ─────────────────────────────────────────────────────────────────────────────
# SYNC HELPERS (per-doctype post-processing)
# ─────────────────────────────────────────────────────────────────────────────
ADDRESS_LOCKED_DOCTYPES = {
    # "External Sales Invoice",
    # "External Delivery Note",
#     "External Sales Order",
#     "External Quotation",
#     "External Purchase Invoice",
#     "External Purchase Order",
#     "External Purchase Receipt",
 }

ADDRESS_FIELDS = [
    "customer_address",
    "shipping_address",
    "shipping_address_name",
    "supplier_address",
    "billing_address",
    "contact_person",
    "contact_display",
    "contact_mobile",
    "contact_email",
    "contact_phone",
]


def _bypass_address_lock(local_dt, doc):
    """
    ERPNext blocks address field changes on submitted docs via validate().
    For already-saved submitted docs, clear address fields directly in DB
    before doc.save() so the validator sees no change.
    """
    if local_dt not in ADDRESS_LOCKED_DOCTYPES:
        return
    if doc.is_new():
        return
    if frappe.utils.cint(doc.get("docstatus", 0)) == 0:
        return

    updates = {}
    for field in ADDRESS_FIELDS:
        if doc.meta.has_field(field):
            updates[field] = None

    if updates:
        frappe.db.set_value(
            local_dt,
            doc.name,
            updates,
            update_modified=False
        )
        for field in updates:
            doc.set(field, None)


def _apply_sync_helpers(local_dt, doc, item, local_fields, company, remote_id):

    if local_dt == "External Account":
        parent_remote = item.get('parent_account')
        if parent_remote:
            parent_name = (
                frappe.db.get_value(local_dt, {"remote_id": parent_remote}, "name")
                or frappe.db.get_value(local_dt, {"name": parent_remote}, "name")
            )
            if not parent_name:
                # ✅ FIX: create stub instead of raising — same pattern as Cost Center
                parent_name = _ensure_tree_parent(
                    local_dt, "parent_account", "account_name", parent_remote, company
                )
            if parent_name:
                doc.parent_account = parent_name
        else:
            # Root level account (ASSETS, Liabilities, etc.)
            doc.parent_account = None

    if local_dt == "External Address":
        if not doc.get("address_title"):
            doc.address_title = (
                item.get("address_title")
                or item.get("address_line1")
                or item.get("name")
            )
        if not doc.get("remote_id"):
            doc.remote_id = remote_id

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

    if local_dt == "External Warehouse":
        parent_remote = item.get("parent_warehouse")
        if parent_remote:
            parent_name = (
                frappe.db.get_value(local_dt, {"remote_id": parent_remote}, "name")
                or frappe.db.get_value(local_dt, {"name": parent_remote}, "name")
            )
            if not parent_name:
                parent_name = _ensure_tree_parent(
                    local_dt, "parent_warehouse", "warehouse_name", parent_remote, company
                )
            if parent_name:
                doc.parent_warehouse = parent_name

    if local_dt == "External Asset":
        if company and 'company' in local_fields and not doc.get('company'):
            doc.company = company

    # if local_dt == "External Material Request":
    #     for row in doc.get("items"):
    #         if not row.warehouse:
    #             row.warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
    if local_dt == "External Purchase Receipt":
        # default_wh = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")

        # for row in doc.get("items"):
        #     if not row.warehouse:
        #         row.warehouse = default_wh

        if not doc.get("company") and company:
            doc.company = company


#def _prepare_child_rows_for_sync(doc, table_fieldname, rows):
    # table_df = doc.meta.get_field(table_fieldname)
    # if not table_df or table_df.fieldtype != "Table" or not table_df.options:
    #     return rows

    # child_meta = frappe.get_meta(table_df.options)
    # child_fieldnames = {df.fieldname for df in child_meta.fields}

    # cleaned_rows = []
    # seen_row_ids = set()
    # seen_row_fingerprints = set()

    # for row in rows:
    #     if not isinstance(row, dict):
    #         cleaned_rows.append(row)
    #         continue

    #     row = dict(row)
    #     remote_child_row_id = row.get("name")
    #     if remote_child_row_id and remote_child_row_id in seen_row_ids:
    #         continue

    #     row.pop("name", None)
    #     row.pop("idx", None)
    #     row.pop("parent", None)
    #     row.pop("parenttype", None)
    #     row.pop("parentfield", None)
    #     row.pop("docstatus", None)
    #     row.pop("owner", None)
    #     row.pop("creation", None)
    #     row.pop("modified", None)
    #     row.pop("modified_by", None)

    #     if "custom_remote_id" in child_fieldnames and remote_child_row_id:
    #         row["custom_remote_id"] = remote_child_row_id

    #     fingerprint_row = dict(row)
    #     fingerprint_row.pop("custom_remote_id", None)
    #     row_fingerprint = json.dumps(fingerprint_row, sort_keys=True, default=str)
    #     if row_fingerprint in seen_row_fingerprints:
    #         continue

    #     if remote_child_row_id:
    #         seen_row_ids.add(remote_child_row_id)
    #     seen_row_fingerprints.add(row_fingerprint)
    #     cleaned_rows.append(row)

    # return cleaned_rows


def _prepare_child_rows_for_sync(doc, table_fieldname, rows):
    table_df = doc.meta.get_field(table_fieldname)
    if not table_df or table_df.fieldtype != "Table" or not table_df.options:
        return rows

    child_meta = frappe.get_meta(table_df.options)
    child_fieldnames = {df.fieldname for df in child_meta.fields}

    cleaned_rows = []
    seen_row_ids = set()
    seen_row_fingerprints = set()

    for row in rows:
        if not isinstance(row, dict):
            cleaned_rows.append(row)
            continue

        row = dict(row)
        remote_child_row_id = row.get("name")

        # True duplicate: same remote child row synced twice in this batch.
        if remote_child_row_id and remote_child_row_id in seen_row_ids:
            continue

        row.pop("name", None)
        row.pop("idx", None)
        row.pop("parent", None)
        row.pop("parenttype", None)
        row.pop("parentfield", None)
        row.pop("docstatus", None)
        row.pop("owner", None)
        row.pop("creation", None)
        row.pop("modified", None)
        row.pop("modified_by", None)

        if "custom_remote_id" in child_fieldnames and remote_child_row_id:
            row["custom_remote_id"] = remote_child_row_id

        if remote_child_row_id:
            # We have a real remote identity for this row - that's the only
            # thing that should decide uniqueness. Two rows CAN legitimately
            # have identical field values (e.g. two JE account lines with the
            # same account/amount), so do NOT drop them based on content.
            seen_row_ids.add(remote_child_row_id)
        else:
            # No remote id to key on at all - content fingerprint is the only
            # fallback available to avoid re-adding an obviously identical
            # row within the same batch.
            fingerprint_row = dict(row)
            fingerprint_row.pop("custom_remote_id", None)
            row_fingerprint = json.dumps(fingerprint_row, sort_keys=True, default=str)
            if row_fingerprint in seen_row_fingerprints:
                continue
            seen_row_fingerprints.add(row_fingerprint)

        cleaned_rows.append(row)

    return cleaned_rows

def _dedupe_saved_child_rows_by_remote_id(local_dt, docname):
    meta = frappe.get_meta(local_dt)
    system_fields = {
        "name", "idx", "parent", "parenttype", "parentfield", "owner",
        "creation", "modified", "modified_by", "docstatus", "doctype"
    }

    for table_df in meta.get_table_fields():
        child_dt = table_df.options
        child_meta = frappe.get_meta(child_dt)
        child_fields = [df.fieldname for df in child_meta.fields]
        if "custom_remote_id" not in child_fields:
            continue

        query_fields = ["name", "idx", "custom_remote_id"] + [
            f for f in child_fields if f not in system_fields and f not in {"custom_remote_id"}
        ]
        rows = frappe.get_all(
            child_dt,
            filters={
                "parent": docname,
                "parenttype": local_dt,
                "parentfield": table_df.fieldname
            },
            fields=query_fields,
            order_by="idx asc, creation asc, name asc"
        )

        seen_remote_ids = set()
        duplicate_names = []
        for row in rows:
            remote_child_id = (row.get("custom_remote_id") or "").strip()
            if not remote_child_id:
                continue
            if remote_child_id in seen_remote_ids:
                duplicate_names.append(row["name"])
                continue
            seen_remote_ids.add(remote_child_id)

        if duplicate_names:
            for duplicate_name in duplicate_names:
                frappe.db.delete(child_dt, {"name": duplicate_name})


COMPANY_PREFIXED_EXTERNAL_DOCTYPES = {
    "External Lead",
    "External Quotation",
    "External Material Request",
    "External Purchase Receipt",
    "External Purchase Invoice",
    "External Stock Entry",
    "External Stock Reconciliation",
    "External Payment Request",
    "External Request for Quotation",
    "External Journal Entry"
}


def _get_company_prefixed_remote_id_for_local_dt(local_dt, remote_id, company):
    if local_dt not in COMPANY_PREFIXED_EXTERNAL_DOCTYPES:
        return remote_id

    company_abbr = _get_company_abbr_for_sync(company)
    return _build_company_prefixed_remote_id(company_abbr, remote_id)


def _get_legacy_unprefixed_external_doc(local_dt, local_fields, original_remote_id, company):
    if local_dt not in COMPANY_PREFIXED_EXTERNAL_DOCTYPES or not original_remote_id:
        return None

    for lookup_field in ("remote_id", "name"):
        filters = {lookup_field: original_remote_id}
        if company and "company" in local_fields:
            filters["company"] = company

        existing_name = frappe.db.get_value(local_dt, filters, "name")
        if existing_name:
            return existing_name

    return None


def _process_item(local_dt, item, idx, local_fields, company, configuration_name, errors, site_url):
    original_remote_id = item.get('name') or item.get('id')
    remote_id = _get_company_prefixed_remote_id_for_local_dt(local_dt, original_remote_id, company)
    remote_docstatus = frappe.utils.cint(item.get("docstatus", 0))
    deferred_fields = {"amended_from"}

    if not remote_id:
        return False

    if remote_id != original_remote_id:
        item = dict(item)
        item["name"] = remote_id
        item["id"] = remote_id

    existing_name = (
        frappe.db.get_value(local_dt, {"remote_id": remote_id}, "name")
        or frappe.db.get_value(local_dt, {"name": remote_id}, "name")
        or _get_legacy_unprefixed_external_doc(local_dt, local_fields, original_remote_id, company)
    )

    # Purge moved inside the savepoint block below so deadlocks on the
    # DELETE roll back cleanly instead of crashing before save even starts.

    # Always create as a brand new document
    doc = frappe.new_doc(local_dt)
    doc.__islocal = True
    doc.name = remote_id
    doc.flags.ignore_naming_series = True
    doc.flags.name_set = True

    # ── Map fields ──
    # Pre-build a set of Attach / Attach Image fieldnames so we can skip
    # remote private-file paths that don't exist on this server.
    _attach_fieldnames = {
        df.fieldname
        for df in frappe.get_meta(local_dt).fields
        if df.fieldtype in ("Attach", "Attach Image")
    }

    for field, value in item.items():
        if field in local_fields and field not in ['name', 'owner', 'creation', 'modified', 'naming_series', 'docstatus'] and field not in deferred_fields:
            if value is not None:
                # Skip attachment fields that reference a remote private/local
                # file path.  Frappe's before_insert tries to open the file on
                # disk, which raises FileNotFoundError because the file only
                # exists on the remote server.  External URLs (http/https) are
                # fine and are left untouched.
                if (
                    field in _attach_fieldnames
                    and isinstance(value, str)
                    and (
                        value.startswith("/private/files/")
                        or value.startswith("/files/")
                    )
                ):
                    continue

                if isinstance(value, list):
                    cleaned_rows = _prepare_child_rows_for_sync(doc, field, value)
                    for row_data in cleaned_rows:
                        doc.append(field, row_data)
                else:
                    doc.set(field, value)

    # ── remote_id & site_url ──
    if 'remote_id' in local_fields:
        doc.remote_id = remote_id
    for site_url_fieldname in ("source_site", "site_url"):
        if site_url_fieldname in local_fields:
            doc.set(site_url_fieldname, site_url)

    # ── Flags ──
    doc.flags.ignore_links = True
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_validate = True

    _apply_sync_helpers(local_dt, doc, item, local_fields, company, remote_id=remote_id)
    _bypass_address_lock(local_dt, doc)

    # ─────────────────────────────────────────────────────────────────
    # SAVE — using a DB savepoint so any failed attempt (link errors,
    # duplicate conflicts, deadlocks, etc.) can be fully rolled back
    # before we either retry the lookup path or give up. This prevents
    # partial child-table inserts that previously caused duplicate rows.
    # The purge of existing rows is included INSIDE the savepoint so a
    # deadlock on the DELETE is also rolled back cleanly.
    # ─────────────────────────────────────────────────────────────────
    savepoint = f"sync_{local_dt.replace(' ', '_')}_{frappe.generate_hash(length=8)}"

    try:
        frappe.db.savepoint(savepoint)

        # Purge existing parent + child rows inside the savepoint so any
        # deadlock on the DELETE rolls back to here rather than leaving
        # partial state. Parallel chunk workers deleting the same rows
        # (full re-sync) trigger InnoDB deadlocks; catching them here lets
        # _run_sync_loop's retry pass re-attempt after other workers finish.
        purge_parent_names = {n for n in (existing_name, remote_id) if n}
        for df in frappe.get_meta(local_dt).get_table_fields():
            for parent_name in purge_parent_names:
                frappe.db.delete(df.options, {
                    "parent": parent_name,
                    "parenttype": local_dt
                })
        if existing_name:
            frappe.db.delete(local_dt, existing_name)

        # Bypass link validation up front (covers child-table links too,
        # which doc.flags.ignore_links alone does not reliably cover).
        frappe.flags.ignore_link_validation = True
        try:
            doc.save()
        except Exception as e:
            # Roll back to before this doc's insert attempt so no
            # partial parent/child rows are left behind.
            frappe.db.sql(f"ROLLBACK TO SAVEPOINT `{savepoint}`")

            if _is_duplicate_conflict(e) and doc.is_new():
                existing = None
                for lookup_field in ["address_title", "name"]:
                    lookup_val = item.get(lookup_field)
                    if lookup_val:
                        existing = frappe.db.get_value(local_dt, {lookup_field: lookup_val}, "name")
                        if existing:
                            break
                if existing:
                    frappe.db.set_value(local_dt, existing, "remote_id", remote_id, update_modified=False)
                    return True
                else:
                    raise
            else:
                raise
        finally:
            frappe.flags.ignore_link_validation = False

        for field in deferred_fields:
            value = item.get(field)
            if field in local_fields and value is not None:
                frappe.db.set_value(doc.doctype, doc.name, field, value, update_modified=False)

        if doc.docstatus != remote_docstatus:
            frappe.db.set_value(doc.doctype, doc.name, "docstatus", remote_docstatus, update_modified=False)

        return True

    except Exception as e:
        # Harden the savepoint rollback: a deadlock forces InnoDB to roll
        # back the entire transaction, destroying all savepoints. If that
        # happens, fall back to a full rollback so the connection stays
        # usable. Without this, the failed ROLLBACK TO SAVEPOINT call
        # would itself raise and crash the whole chunk.
        try:
            frappe.db.sql(f"ROLLBACK TO SAVEPOINT `{savepoint}`")
        except Exception:
            try:
                frappe.db.rollback()
            except Exception:
                pass

        # Deadlock: InnoDB already rolled us back. Return False so the
        # per-chunk retry loop re-attempts this record after other workers
        # have released their locks. Do NOT re-raise — that would crash
        # the entire chunk and lose all items processed after this one.
        if isinstance(e, frappe.QueryDeadlockError):
            errors.append({"remote_id": remote_id, "error": f"Deadlock (will retry): {str(e)}"})
            return False

        errors.append({"remote_id": remote_id, "error": str(e)})
        return False
# ─────────────────────────────────────────────────────────────────────────────
# SHARED SYNC LOOP
# ─────────────────────────────────────────────────────────────────────────────

def _run_sync_loop(data, local_dt, local_fields, company, configuration_name, site_url):
    """
    Return values from _process_item:
      True  → saved/updated this run  → count++
      False → genuine failure         → added to not_saved report
    """
    count = 0
    errors = []
    not_saved = []

    for idx, item in enumerate(data, start=1):
        result = _process_item(local_dt, item, idx, local_fields, company, configuration_name, errors, site_url)
        if result:
            count += 1
        else:
            not_saved.append(item)

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
                "filters": json.dumps([["docstatus", "in", [0, 1, 2]]]), 
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
        data, local_dt, local_fields, company, configuration_name,site_url
    )

    # ✅ Retry pass — catches any edge cases left from first run
    if not_saved:
        retry_count, retry_errors, not_saved = _run_sync_loop(
            not_saved, local_dt, local_fields, company, configuration_name,site_url
        )
        count += retry_count
        errors.extend(retry_errors)

    last_sync = frappe.utils.now()
    if child_docname:
        frappe.db.set_value("External Site Configuration CT", child_docname, "last_sync", last_sync)

    return {
        "count": count,
        "fatal_error": len(not_saved) > 0 and len(errors) == 0,
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
def sync__docs(site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None):
    dedicated_sync_methods = {
        "Lead": "emkan_insights.emkan_insights.sync_leads_from_remote.sync_leads_from_remote",
        "Quotation": "emkan_insights.emkan_insights.sync_quotation_from_remote.sync_quotations_from_remote",
        "Material Request": "emkan_insights.emkan_insights.sync_material_requests_from_remote.sync_material_requests_from_remote",
        "Cost Center": "emkan_insights.emkan_insights.sync_cost_centers_from_remote.sync_cost_centers_from_remote",
        "Purchase Receipt": "emkan_insights.emkan_insights.sync_purchase_receipt_from_remote.sync_purchase_receipts_from_remote",
        "Purchase Invoice": "emkan_insights.emkan_insights.sync_purchase_invoice_from_remote.sync_purchase_invoices_from_remote",
        "Stock Entry": "emkan_insights.emkan_insights.sync_stock_entry_from_remote.sync_stock_entries_from_remote",
        "Stock Reconciliation": "emkan_insights.emkan_insights.sync_stock_reconciliation_from_remote.sync_stock_reconciliations_from_remote",
        "Payment Request": "emkan_insights.emkan_insights.sync_payment_request_from_remote.sync_payment_requests_from_remote",
        "Request for Quotation": "emkan_insights.emkan_insights.sync_request_for_quotation_from_remote.sync_request_for_quotation_from_remote", 
        "Journal Entry":  "emkan_insights.emkan_insights.sync_journal_entry_from_remote.sync_journal_entries_from_remote",
    }
    sync_method = dedicated_sync_methods.get(
        ref_doctype,
        "emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.sync_data_from_remote",
    )

    frappe.enqueue(
        sync_method,
        site_url=site_url,
        api_key=api_key,
        api_secret=api_secret,
        ref_doctype=ref_doctype,
        child_docname=child_docname,
        company=company,
        configuration_name=configuration_name,
        queue="long",
        timeout=2000
    )
    return "Sync Queued"


def _get_company_abbr_for_sync(company):
    if not company:
        frappe.throw(_("Company is required for this sync."))

    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        frappe.throw(_("Company {0} has no abbreviation set.").format(company))

    return abbr


def _build_company_prefixed_remote_id(company_abbr, remote_id):
    remote_id = (remote_id or "").strip()
    if not remote_id:
        return remote_id

    if remote_id.startswith(f"{company_abbr}-"):
        return remote_id

    return f"{company_abbr}-{remote_id}"

# ─────────────────────────────────────────────────────────────────────────────
# GENERIC CHUNK-BASED SYNC  (DEFAULT_FETCHER path)
# Mirrors the MR / JE / RFQ chunk pattern so every doctype is handled with
# bounded per-job timeouts regardless of total remote record count.
# ─────────────────────────────────────────────────────────────────────────────

GENERIC_CHUNK_SIZE = 300
GENERIC_FETCH_WORKERS = 8


def _generic_progress_key(child_docname):
    return f"generic_sync_progress:{child_docname}"


def _init_generic_progress(child_docname, total_chunks):
    frappe.cache().set_value(_generic_progress_key(child_docname), json.dumps({
        "total_chunks": total_chunks,
        "completed_chunks": 0,
        "count": 0,
        "not_saved_count": 0,
        "errors": [],
    }))


def _update_generic_progress(child_docname, count_delta, not_saved_delta, chunk_errors):
    key = _generic_progress_key(child_docname)
    raw = frappe.cache().get_value(key)
    state = json.loads(raw) if raw else {
        "total_chunks": 0, "completed_chunks": 0, "count": 0,
        "not_saved_count": 0, "errors": [],
    }
    state["completed_chunks"] += 1
    state["count"] += count_delta
    state["not_saved_count"] += not_saved_delta
    state["errors"].extend(chunk_errors)
    frappe.cache().set_value(key, json.dumps(state))
    return state


def _get_generic_progress_state(child_docname):
    raw = frappe.cache().get_value(_generic_progress_key(child_docname))
    if raw:
        return json.loads(raw)
    return {"total_chunks": 0, "completed_chunks": 0, "count": 0, "not_saved_count": 0, "errors": []}


def _fetch_remote_names_generic(base_url, headers, remote_dt, filters):
    """
    Cheap listing pass: fetches only `name` fields so we can plan chunks
    before doing any per-record HTTP work.
    """
    encoded_dt = quote(str(remote_dt), safe="")
    url = f"{base_url}/api/resource/{encoded_dt}"
    names = []
    limit = 1000
    start = 0

    while True:
        params = {
            "fields": json.dumps(["name"]),
            "filters": json.dumps(filters),
            "limit_page_length": limit,
            "limit_start": start,
        }
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        page = response.json().get("data", [])
        if not page:
            break
        names.extend(row["name"] for row in page if row.get("name"))
        if len(page) < limit:
            break
        start += limit

    return names


def _fetch_generic_docs_threaded(base_url, headers, remote_dt, names):
    """
    Threaded per-record GET for a chunk of names.
    8 concurrent connections are used so chunk HTTP time stays bounded.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    docs = []

    def _fetch_one(name):
        encoded_dt = quote(str(remote_dt), safe="")
        encoded_name = quote(str(name), safe="")
        url = f"{base_url}/api/resource/{encoded_dt}/{encoded_name}"
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json().get("data") or {"name": name}
        except Exception as e:
            frappe.logger("external_sync").warning(
                "Failed to fetch full doc for %s/%s: %s", remote_dt, name, str(e)
            )
            return {"name": name}

    with ThreadPoolExecutor(max_workers=GENERIC_FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, name): name for name in names}
        for future in as_completed(futures):
            docs.append(future.result())

    return docs


# doctype_map shared between orchestrator and worker (kept as a module-level
# constant so it's defined exactly once and both functions reference the same
# source of truth).
_GENERIC_DOCTYPE_MAP = {
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
    "Customer Group": "External Customer Group",
    "Item": "External Item",
    "Item Group": "External Item Group",
    "Item Tax Template": "External Item Tax Template",
    "Location": "External Location",
    "Manufacturer": "External Manufacturer",
    "Price List": "External Price List",
    "Project Type": "External Project Type",
    "Supplier": "External Supplier",
    "BOM": "External BOM",
    "Production Plan": "External Production Plan",
    "Work Order": "External Work Order",
    "Tax Category": "External Tax Category",
    "Supplier Group": "External Supplier Group",
    "Territory": "External Territory",
    "UOM": "External Uom",
    "Warehouse": "External Warehouse",
    "Purchase Invoice": "External Purchase Invoice",
    "Payment Entry": "External Payment Entry",
    "Purchase Order": "External Purchase Order",
    "Sales Order": "External Sales Order",
    "Stock Entry": "External Stock Entry",
    "Stock Reconciliation": "External Stock Reconciliation",
    "Project": "External Project",
    "Request for Quotation": "External Request for Quotation",
    "Supplier Quotation": "External Supplier Quotation",
    "Employee": "External Employee",
    "Item Price": "External Item Price",
    "Department": "External Department",
    "Designation": "External Designation",
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
    "Material Request": "External Material Request",
    "Prospect": "External Prospect",
    "Sales Stage": "External Sales Stage",
    "Payment Term": "External Payment Term",
    "Mode of Payment": "External Mode of Payment",
    "Lead Source": "External Lead Source",
    "Email Template": "External Email Template",
    "Lead": "External Lead",
    "Opportunity": "External Opportunity",
    "Vehicle": "External Vehicle",
    "Batch Plant": "External Batch Plant",
    "Payment Request": "External Payment Request",
}


@frappe.whitelist()
def sync_data_from_remote(site_url, api_key, api_secret, ref_doctype, child_docname, company=None, configuration_name=None):
    """
    Orchestrator (fast entry point).

    1. Resolves remote_dt → local_dt via _GENERIC_DOCTYPE_MAP.
    2. Fetches only `name` fields from remote (cheap listing pass) with the
       incremental last_sync filter when available.
    3. Splits names into GENERIC_CHUNK_SIZE = 300 chunks.
    4. Enqueues one process_generic_chunk background job per chunk.
    5. Returns immediately — no record-level processing happens here.

    The sync lock is acquired here and released by the last chunk worker.
    """
    frappe.logger("external_sync").info(
        "Generic chunk-sync orchestrator: %s (company=%s)", ref_doctype, company
    )

    if not _acquire_sync_lock(child_docname):
        frappe.throw(_("A sync is already running for this row. Please wait for it to finish."))

    try:
        remote_dt = ref_doctype
        local_dt = _GENERIC_DOCTYPE_MAP.get(remote_dt)

        if not local_dt:
            frappe.log_error(
                title="Sync Data Error",
                message=f"Doctype mapping not found for: {ref_doctype}"
            )
            frappe.throw(_("Mapping for {0} not found").format(ref_doctype))

        if not frappe.db.exists("DocType", local_dt):
            frappe.throw(
                _("Local DocType {0} is not installed. Run bench migrate and reload.")
                .format(local_dt)
            )

        base_url = (site_url or "").rstrip("/")
        headers = {
            "Authorization": f"token {api_key}:{api_secret}",
            "Content-Type": "application/json",
        }
        fetch_started_at = frappe.utils.now()

        previous_last_sync = frappe.db.get_value(
            "External Site Configuration CT", child_docname, "last_sync"
        )

        # Tree/master doctypes (Item Group, UOM, Warehouse, etc.) have no
        # docstatus column.  Filtering on it causes the remote Frappe REST API
        # to silently return an empty list — the job registers but fetches
        # nothing and produces no error.  Only add the docstatus filter for
        # submittable doctypes.
        _NON_SUBMITTABLE_REMOTE_DOCTYPES = {
            "Item Group", "Item Tax Template", "UOM", "Territory",
            "Warehouse", "Cost Center", "Price List", "Tax Category",
            "Supplier Group", "Customer Group", "Sales Stage",
            "Lead Source", "Project Type", "Payment Term",
            "Mode of Payment", "Designation", "Department",
            "Manufacturer", "Location", "Batch Plant",
            "Sales Taxes and Charges Template",
            "Purchase Taxes and Charges Template",
        }
        filters = []
        if remote_dt not in _NON_SUBMITTABLE_REMOTE_DOCTYPES:
            filters.append(["docstatus", "in", [0, 1, 2]])
        # if previous_last_sync:
        #     # filters.append([
        #     #     "modified", ">", frappe.utils.get_datetime_str(previous_last_sync)
        #     # ])

        names = _fetch_remote_names_generic(base_url, headers, remote_dt, filters)

        if not names:
            _release_sync_lock(child_docname)
            frappe.db.set_value(
                "External Site Configuration CT", child_docname, "last_sync", fetch_started_at
            )
            return {
                "count": 0, "fetched_total": 0, "not_saved_count": 0,
                "last_sync": fetch_started_at, "errors": [],
            }

        chunks = [names[i:i + GENERIC_CHUNK_SIZE] for i in range(0, len(names), GENERIC_CHUNK_SIZE)]
        _init_generic_progress(child_docname, len(chunks))

        for idx, chunk in enumerate(chunks):
            frappe.enqueue(
                "emkan_insights.emkan_insights.doctype.external_site_configuration"
                ".external_site_configuration.process_generic_chunk",
                queue="long",
                timeout=900,
                job_name=f"generic_sync_chunk_{child_docname}_{idx}",
                site_url=site_url,
                api_key=api_key,
                api_secret=api_secret,
                company=company,
                configuration_name=configuration_name,
                child_docname=child_docname,
                ref_doctype=ref_doctype,
                local_dt=local_dt,
                remote_dt=remote_dt,
                remote_names=chunk,
                is_last_chunk=(idx == len(chunks) - 1),
                fetch_started_at=fetch_started_at,
            )

        return (
            f"Queued {len(chunks)} chunk(s) covering {len(names)} {ref_doctype} records"
        )

    except Exception:
        _release_sync_lock(child_docname)
        raise


@frappe.whitelist()
def process_generic_chunk(
    site_url, api_key, api_secret, company, configuration_name,
    child_docname, ref_doctype, local_dt, remote_dt,
    remote_names, is_last_chunk=False, fetch_started_at=None,
):
    """
    Background worker for a single chunk of generic-path doctype records.

    Steps:
      1. Fetch full docs (with child tables) for this chunk — threaded.
      2. Apply hierarchy sorting for Account / Cost Center / Item Group.
      3. Save to local External doctype via _run_sync_loop.
      4. Retry pass for transient failures (deadlocks, etc.).
      5. Update Redis progress.
      6. If last chunk: write last_sync, release sync lock, publish realtime.
    """
    base_url = (site_url or "").rstrip("/")
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
    }
    state = None

    try:
        # Phase 1 — fetch full docs for this chunk (threaded HTTP)
        docs = _fetch_generic_docs_threaded(base_url, headers, remote_dt, remote_names)

        # Phase 2 — hierarchy sort (must happen per-chunk for tree doctypes;
        # stub-parent creation in _process_item handles cross-chunk ordering)
        if local_dt == "External Account":
            docs = _sort_accounts_by_reference_order(docs)
        elif local_dt == "External Cost Center":
            docs = _sort_by_hierarchy(docs, "parent_cost_center")
        elif local_dt == "External Item Group":
            docs = _sort_by_hierarchy(docs, "parent_item_group")
        elif local_dt == "External Warehouse":
            docs = _sort_warehouses_by_hierarchy(docs)

        # Phase 3 — save
        local_meta = frappe.get_meta(local_dt)
        local_fields = [df.fieldname for df in local_meta.fields]

        count, errors, not_saved = _run_sync_loop(
            docs, local_dt, local_fields, company, configuration_name, site_url
        )

        # Phase 4 — retry pass for transient failures
        if not_saved:
            retry_count, retry_errors, not_saved = _run_sync_loop(
                not_saved, local_dt, local_fields, company, configuration_name, site_url
            )
            count += retry_count
            errors.extend(retry_errors)

        state = _update_generic_progress(child_docname, count, len(not_saved), errors)

    except Exception:
        error_message = frappe.get_traceback()
        frappe.logger("external_sync").error(
            "Generic chunk failed for %s/%s: %s", ref_doctype, child_docname, error_message
        )
        state = _update_generic_progress(
            child_docname, 0, len(remote_names),
            [{"remote_id": None, "error": f"Chunk failed: {error_message}"}],
        )
        raise

    finally:
        if is_last_chunk:
            if state is None:
                state = _get_generic_progress_state(child_docname)

            has_failures = (state or {}).get("not_saved_count", 0) > 0

            if has_failures:
                # ⚠️  Some records failed to save even after the retry pass.
                # Do NOT advance last_sync — leaving it unchanged forces the next
                # sync to do a full re-fetch, giving those records another attempt.
                # (Advancing last_sync here would permanently hide them from
                # incremental syncs because their `modified` timestamp on the
                # remote is older than fetch_started_at.)
                frappe.logger("external_sync").warning(
                    "Not advancing last_sync for %s/%s — %d record(s) failed to save.",
                    ref_doctype, child_docname, has_failures,
                )
                last_sync = frappe.db.get_value(
                    "External Site Configuration CT", child_docname, "last_sync"
                )
            else:
                last_sync = fetch_started_at or frappe.utils.now()
                frappe.db.set_value(
                    "External Site Configuration CT", child_docname, "last_sync", last_sync
                )

            _release_sync_lock(child_docname)
            frappe.publish_realtime(
                "generic_sync_complete",
                {
                    "child_docname": child_docname,
                    "ref_doctype": ref_doctype,
                    "count": state.get("count"),
                    "not_saved_count": state.get("not_saved_count"),
                    "errors": state.get("errors"),
                    "last_sync": last_sync,
                },
            )







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
            row.pop("name", None)
            row.pop("parent", None)
            row.pop("parenttype", None)
            row.pop("parentfield", None)
            cleaned.append(row)
        doc.set(table_field.fieldname, cleaned)
