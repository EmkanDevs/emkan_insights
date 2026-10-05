import json
import frappe
from frappe import _
from frappe.utils import flt, cint
from typing import Union, List  # This fixes the NameError: Union

from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.tree_sync import order_tree_sync_names

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "__last_sync_on"
}

COMPANY_PREFIXED_TARGETS = {
    "Customer Group",
    "Supplier Group",
}

COMPANY_PREFIXED_LINKS = COMPANY_PREFIXED_TARGETS | {
    "Item",
    "Warehouse",
}


def _safe_log_external_sync_error(message, *args):
    try:
        frappe.logger("external_sync").error(message, *args)
    except Exception:
        pass


def _safe_log_error(message=None, title=None):
    try:
        frappe.log_error(message=message, title=title)
    except Exception:
        pass


def _safe_log(level, message):
    try:
        getattr(frappe.logger("external_sync"), level)(message)
    except Exception:
        pass

@frappe.whitelist()
def sync_account_docs(source_doctype: str, names, company: str):

    if isinstance(names, str):
        names = json.loads(names)

    for name in names:
        sync_external_account(name, company)

    return "Success"
def sync_doc_by_key(
    source_doctype: str,
    source_name: str,
    target_doctype: str,
    key_field: str,
    *,
    title_field_source: str | None = None,
    extra_ignore_fields: set | None = None,
    mandatory_defaults: dict | None = None,
):
    src = frappe.get_doc(source_doctype, source_name)
    key_value = src.get(key_field)
    info = get_external_sync_info(source_doctype)
    company_abbr = _get_source_company_abbr(src)

    enforce_remote_name = (
        info
        and info.get("force_id")
        and key_field == "remote_id"
        and bool(key_value)
    )
    # enforce_remote_name = (
    #     target_doctype == "Supplier"
    #     and key_field == "remote_id"
    #     and bool(key_value)
    # )

    if not key_value:
        frappe.throw(f"{key_field} is mandatory to sync {target_doctype}")

    if target_doctype in COMPANY_PREFIXED_TARGETS:
        prefixed_key = key_value
        if not prefixed_key and title_field_source:
            prefixed_key = src.get(title_field_source)
        key_value = _build_company_prefixed_name(company_abbr, prefixed_key or src.name)

    target_meta = frappe.get_meta(target_doctype)
    target_fields = {df.fieldname for df in target_meta.fields}

    actual_target_key = _get_actual_target_key(target_doctype, key_field)
    
    existing_name = None
    if actual_target_key:
        existing_name = frappe.db.get_value(target_doctype, {actual_target_key: key_value}, "name")
    elif frappe.db.exists(target_doctype, key_value):
        # Fallback: if the remote key already exists as document name, update it.
        existing_name = key_value

    # Tax ID matching for Customer/Supplier: merge if same tax_id exists
    if not existing_name and target_doctype in ("Customer", "Supplier"):
        src_tax_id = src.get("tax_id")
        if src_tax_id:
            existing_name = frappe.db.get_value(
                target_doctype, {"tax_id": src_tax_id}, "name"
            )

    # Additional fallback for doctypes where naming is based on title/source field
    # (for example UOM.name from uom_name), while remote_id is different.
    if not existing_name and title_field_source and target_doctype not in ({"Asset"} | COMPANY_PREFIXED_TARGETS):
        title_candidate = src.get(title_field_source)
        if title_candidate and frappe.db.exists(target_doctype, title_candidate):
            existing_name = title_candidate

    if existing_name:
        tgt = frappe.get_doc(target_doctype, existing_name)
        is_new = False
    else:
        tgt = frappe.new_doc(target_doctype)
        is_new = True
        if enforce_remote_name:
            tgt.name = key_value
            tgt.flags.name_set = True   # 🔥 IMPORTANT: prevent autoname override
        # if enforce_remote_name:
        #     tgt.set_new_name(key_value)
        #     tgt.name = key_value

    # if target_doctype == "Item Group" and _is_root_item_group_doc(tgt=tgt, src=src, title_field_source=title_field_source):
    #     return {
    #         "doctype": target_doctype,
    #         "name": tgt.name or src.get(title_field_source) or src.name,
    #         "action": "skipped",
    #     }
    if target_doctype == "Item Group" and _is_root_item_group_doc(
        tgt=tgt, src=src, title_field_source=title_field_source
    ):
        root_name = "All Item Groups"

        if not frappe.db.exists("Item Group", root_name):
            root = frappe.get_doc({
                "doctype": "Item Group",
                "name": root_name,
                "item_group_name": root_name,
                "is_group": 1
            })
            root.flags.name_set = True
            if actual_target_key:
                root.set(actual_target_key, key_value)
            root.insert(ignore_permissions=True)
        else:
            values = {"is_group": 1}
            if actual_target_key:
                values[actual_target_key] = key_value
            frappe.db.set_value("Item Group", root_name, values, update_modified=False)

        return {
            "doctype": target_doctype,
            "name": root_name,
            "action": "ensured",
        }
    if target_doctype == "Supplier Group" and _is_root_supplier_group_doc(
        tgt=tgt, src=src, title_field_source=title_field_source
    ):
        root_name = _build_company_prefixed_name(company_abbr, "All Supplier Groups")

        if not frappe.db.exists("Supplier Group", root_name):
            root = frappe.get_doc({
                "doctype": "Supplier Group",
                "name": root_name,
                "supplier_group_name": root_name,
                "is_group": 1,
                "parent_supplier_group": None,
            })
            root.flags.name_set = True
            if actual_target_key:
                root.set(actual_target_key, key_value)
            root.insert(ignore_permissions=True)
        else:
            values = {
                "parent_supplier_group": None,
                "old_parent": None,
                "is_group": 1,
            }
            if actual_target_key:
                values[actual_target_key] = key_value
            frappe.db.set_value(
                "Supplier Group",
                root_name,
                values,
                update_modified=False,
            )

        return {
            "doctype": target_doctype,
            "name": root_name,
            "action": "ensured",
        }

    if target_doctype == "Territory" and _is_root_territory_doc(
        tgt=tgt, src=src, title_field_source=title_field_source
    ):

        if not frappe.db.exists("Territory", "All Territories"):

            root = frappe.get_doc({
                "doctype": "Territory",
                "territory_name": "All Territories",
                "is_group": 1,
                "parent_territory": None   # ⭐ VERY IMPORTANT
            })

            if actual_target_key:
                root.set(actual_target_key, key_value)

            root.insert(ignore_permissions=True)

        else:
            # Use db.set_value to bypass ERPNext's nested-set validate_loop,
            # which raises "Item cannot be added to its own descendants" when
            # root.save() triggers a nestedset rebuild.  This mirrors the
            # Supplier Group root-update pattern exactly.
            values = {
                "parent_territory": None,
                "old_parent": None,
                "is_group": 1,
            }
            if actual_target_key:
                values[actual_target_key] = key_value
            frappe.db.set_value(
                "Territory",
                "All Territories",
                values,
                update_modified=False,
            )

        return {
            "doctype": target_doctype,
            "name": "All Territories",
            "action": "ensured",
        }

    ignore = SYSTEM_FIELDS | (extra_ignore_fields or set()) | {"remote_id", "status"}
    if frappe.utils.cint(getattr(target_meta, "is_tree", 0)):
        # Never map nested-set internals from external payloads.
        ignore |= {"lft", "rgt", "old_parent"}

    _sync_child_tables(src, tgt, target_meta, company_abbr=company_abbr)

    for field, value in src.as_dict().items():
        # Sync child tables
        # if target_doctype in ["Purchase Invoice", "Payment Entry"]:

        #     _sync_child_tables(
        #         src,
        #         tgt,
        #         target_meta
        #     )
        if field in ignore:
            continue

        target_field = field
        if field not in target_fields and f"custom_{field}" in target_fields:
            target_field = f"custom_{field}"

        if target_doctype == "Account" and target_field == "parent_account":
            # External Account.parent_account points to External Account rows,
            # so resolve it explicitly after base field mapping.
            continue
        if target_doctype == "Item Group" and target_field == "parent_item_group":
            # External Item Group.parent_item_group may reference source keys/names,
            # so resolve it explicitly after base field mapping.
            continue
        if target_doctype == "Supplier Group" and target_field == "parent_supplier_group":
            # External Supplier Group.parent_supplier_group may reference source keys/names,
            # so resolve it explicitly after base field mapping.
            continue
        if target_doctype == "Customer Group" and target_field == "parent_customer_group":
            continue
        if target_doctype == "Territory" and target_field == "parent_territory":
            # External Territory.parent_territory may reference source keys/names,
            # so resolve it explicitly after base field mapping.
            continue

        if target_doctype == "Contract" and field in ["party_name", "party_type"]:
            continue

        if target_field in target_fields:
            df = target_meta.get_field(target_field)
            if df and df.fieldtype == "Table":
                continue
            if df and df.fieldtype == "Link" and value:
                resolved = resolve_link_value(df.options, value, company_abbr=company_abbr)
                if resolved:
                    value = resolved

            tgt.set(target_field, value)

    if actual_target_key:
        tgt.set(actual_target_key, key_value)

    mandatory_defaults = _resolve_mandatory_defaults(
        target_doctype=target_doctype,
        defaults=mandatory_defaults,
        company_abbr=company_abbr,
    )

    for field, value in mandatory_defaults.items():
        if not tgt.get(field):
            tgt.set(field, value)

    if title_field_source and target_meta.title_field:
        tgt.set(target_meta.title_field, src.get(title_field_source) or key_value)

    if target_doctype == "Supplier":
        _apply_supplier_sync_rules(tgt=tgt)

    tgt.flags.ignore_links = True

    if target_doctype == "Contract":

        party_type = src.get("party_type")
        party_name = src.get("party_name")

        tgt.party_type = party_type

        resolved = None

        if party_type and party_name:
            resolved = frappe.db.get_value(
                party_type,
                {"remote_id": party_name},
                "name"
            )

        if not resolved and frappe.db.exists(party_type, party_name):
            resolved = party_name

        if not resolved and party_type == "Customer":
            sync_doc_by_key(
                source_doctype="External Customer",
                source_name=party_name,
                target_doctype="Customer",
                key_field=key_field,
                title_field_source="customer_name"
            )

            resolved = frappe.db.get_value(
                "Customer",
                {"remote_id": party_name},
                "name"
            )

        if not resolved:
            frappe.throw(f"Customer not found for Contract: {party_name}")

        # ✅ MOVE HERE
        tgt.party_name = resolved

    if target_doctype == "Account":
        _apply_account_sync_rules(src=src, tgt=tgt, key_field=key_field)
        # External sync can create accounts directly in child companies;
        # bypass root-company enforcement during this controlled import path.
        tgt.flags.ignore_root_company_validation = True
    elif target_doctype == "Item Group":
        _apply_item_group_sync_rules(
            src=src,
            tgt=tgt,
            key_field=key_field,
            title_field_source=title_field_source,
            company_abbr=company_abbr,
        )
    elif target_doctype == "Contact":
        _apply_contact_sync_rules(tgt)
    elif target_doctype == "Territory":
        _apply_territory_sync_rules(
            src=src,
            tgt=tgt,
            key_field=key_field,
            title_field_source=title_field_source,
        )
    elif target_doctype == "Location":
        _apply_location_sync_rules(
            src=src,
            tgt=tgt,
            key_field=key_field,
            title_field_source=title_field_source,
        )
    elif target_doctype == "Supplier Group":
        _apply_supplier_group_sync_rules(
            src=src,
            tgt=tgt,
            key_field=key_field,
            title_field_source=title_field_source,
            company_abbr=company_abbr,
        )
    elif target_doctype == "Customer Group":
        _apply_customer_group_sync_rules(
            src=src,
            tgt=tgt,
            key_field=key_field,
            title_field_source=title_field_source,
            company_abbr=company_abbr,
        )

    elif target_doctype == "Sales Person":
        _apply_sales_person_sync_rules(tgt)

    elif target_doctype == "Asset":
        _apply_asset_sync_rules(src=src, tgt=tgt, key_field=key_field)
        _save_and_sync_docstatus(tgt, cint(src.get("docstatus")))

        return {
            "doctype": "Asset",
            "name": tgt.name,
            "action": "created" if is_new else "updated",
        }


    elif target_doctype == "Purchase Invoice":
        _apply_purchase_invoice_sync_rules(src, tgt, key_field)

    elif target_doctype == "Payment Entry":
        _apply_payment_entry_sync_rules(src, tgt, key_field)
    
    elif target_doctype == "Bank Account":
        _apply_bank_account_sync_rules(src, tgt, key_field)

    elif target_doctype == "Expense Claim":
        _apply_expense_claim_sync_rules(src, tgt)

    elif target_doctype == "Address":
        _apply_address_sync_rules(src, tgt)

    elif target_doctype == "Asset Category":
        _apply_asset_category_sync_rules(src, tgt)

    try:
        tgt.save(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # Skip if a concurrent/alternate naming path has already created it.
        if frappe.db.exists(target_doctype, tgt.name):
            return {
                "doctype": target_doctype,
                "name": tgt.name,
                "action": "skipped",
            }
        raise

    if (
        enforce_remote_name
        and not is_new
        and tgt.name != key_value
        and not frappe.db.exists(target_doctype, key_value)
    ):
        tgt_name_before_rename = tgt.name
        renamed_name = frappe.rename_doc(target_doctype, tgt_name_before_rename, key_value, force=True)
        tgt = frappe.get_doc(target_doctype, renamed_name)

    return {
        "doctype": target_doctype,
        "name": tgt.name,
        "action": "created" if is_new else "updated",
    }

def _apply_address_sync_rules(src, tgt):
	pass


def _apply_sales_person_sync_rules(tgt):
    # Sales Person "targets" child table links to Fiscal Year and Monthly
    # Distribution, which often don't exist locally for remote records.
    # We don't care about target/quota data for synced sales people, so
    # just drop those rows and bypass link validation as a safety net.
    tgt.set("targets", [])

def _apply_asset_category_sync_rules(src, tgt):
    account_fields = (
        "fixed_asset_account",
        "accumulated_depreciation_account",
        "depreciation_expense_account",
        "capital_work_in_progress_account",
    )

    for row in tgt.get("accounts", []):
        if not row.company_name:
            continue

        company_abbr = frappe.db.get_value("Company", row.company_name, "abbr")
        for fieldname in account_fields:
            account = row.get(fieldname)
            if account:
                row.set(
                    fieldname,
                    resolve_company_account(account, row.company_name, company_abbr),
                )


def _apply_contact_sync_rules(tgt):
    # Bypass strict validations

    # Remove problematic link fields
    tgt.lead = None
    tgt.prospect = None

    # Fix: ensure only one email is marked as primary
    if tgt.get("email_ids"):
        found_primary = False
        for row in tgt.email_ids:
            if row.get("is_primary"):
                if found_primary:
                    row.is_primary = 0  # unset duplicate primaries
                else:
                    found_primary = True
        # If none were marked primary, mark the first one
        if not found_primary and tgt.email_ids:
            tgt.email_ids[0].is_primary = 1

    # Fix: ensure only one phone is marked as primary
    if tgt.get("phone_nos"):
        found_primary = False
        for row in tgt.phone_nos:
            if row.get("is_primary_phone"):
                if found_primary:
                    row.is_primary_phone = 0
                else:
                    found_primary = True

    
def ensure_territory_root():
    if not frappe.db.exists("Territory", "All Territories"):
        frappe.get_doc({
            "doctype": "Territory",
            "territory_name": "All Territories",
            "is_group": 1
        }).insert(ignore_permissions=True)


@frappe.whitelist()
def sync_external_docs(
    source_doctype: str,
    target_doctype: str = None,
    names: Union[str, List[str]] = None,
    key_field: str = "remote_id",
    title_field_source: str | None = None,
    mandatory_defaults: dict | None = None,
    sync_now: bool = False,
):
    """
    Thin HTTP entry-point — resolves metadata and enqueues the actual work as
    a background job so the gunicorn worker is never held for longer than a
    few milliseconds.  Returns immediately with a "Queued" message.

    The heavy lifting is done by _sync_external_docs_worker (below).
    """
    if isinstance(names, str):
        names = json.loads(names)

    if not target_doctype or not title_field_source:
        info = get_external_sync_info(source_doctype)
        if info:
            target_doctype = target_doctype or info["target"]
            title_field_source = title_field_source or info["title_field"]
            if mandatory_defaults is None:
                mandatory_defaults = info["defaults"]

    if not target_doctype:
        frappe.throw(f"Target doctype could not be determined for {source_doctype}")

    if sync_now:
        return _sync_external_docs_worker(
            source_doctype=source_doctype,
            target_doctype=target_doctype,
            names=names,
            key_field=key_field,
            title_field_source=title_field_source,
            mandatory_defaults=mandatory_defaults,
            raise_on_error=True,
        )

    frappe.enqueue(
        "emkan_insights.emkan_insights.external_sync._sync_external_docs_worker",
        queue="long",
        timeout=900,
        job_name=f"sync_external_docs_{source_doctype}",
        source_doctype=source_doctype,
        target_doctype=target_doctype,
        names=names,
        key_field=key_field,
        title_field_source=title_field_source,
        mandatory_defaults=mandatory_defaults,
    )

    return f"Queued sync of {len(names)} {source_doctype} record(s) to {target_doctype}"


def _sync_external_docs_worker(
    source_doctype: str,
    target_doctype: str,
    names: List[str],
    key_field: str = "remote_id",
    title_field_source: str | None = None,
    mandatory_defaults: dict | None = None,
    raise_on_error: bool = False,
):
    """
    Background worker — does the actual per-record sync loop.
    Called exclusively via frappe.enqueue from sync_external_docs.
    """
    # Tree ordering can add ancestors, but callers validate against their original plan.
    requested_names = set(names)
    target_meta = frappe.get_meta(target_doctype)
    parent_field = getattr(target_meta, "nsm_parent_field", None)
    if frappe.utils.cint(getattr(target_meta, "is_tree", 0)) and parent_field:
        names = order_tree_sync_names(
            source_doctype,
            names,
            parent_field,
            key_field=key_field,
        )

    # 🔥 Ensure Territory Root before syncing
    if target_doctype == "Territory":
        ensure_territory_root()

    results = []
    errors = []
    synced_source_names = []
    for source_name in names:
        savepoint = f"external_sync_{frappe.generate_hash(length=10)}"
        try:
            frappe.db.savepoint(savepoint)
            res = sync_doc_by_key(
                source_doctype=source_doctype,
                source_name=source_name,
                target_doctype=target_doctype,
                key_field=key_field,
                title_field_source=title_field_source,
                mandatory_defaults=mandatory_defaults,
            )
            synced_source_names.append(source_name)
            if source_name in requested_names:
                results.append(res["name"])
        except Exception as e:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
            errors.append({"name": source_name, "error": str(e)})
            _safe_log_external_sync_error(
                "sync_external_docs failed for %s/%s: %s",
                source_doctype, source_name, frappe.get_traceback(),
            )
            break

    _mark_external_docs_synced(source_doctype, synced_source_names)

    if errors:
        _safe_log_error(
            title=f"Sync errors: {source_doctype} → {target_doctype}",
            message="\n".join(f"{e['name']}: {e['error']}" for e in errors),
        )
        if raise_on_error:
            first_error = errors[0]
            frappe.throw(
                f"Failed to sync {source_doctype} {first_error['name']}: {first_error['error']}"
            )

    return results


def _mark_external_docs_synced(source_doctype: str, source_names: List[str]) -> None:
    """Clear successfully processed staging records from the pending-sync query."""
    if not source_names or not frappe.get_meta(source_doctype).has_field("last_synced_on"):
        return

    source = frappe.qb.DocType(source_doctype)
    (
        frappe.qb.update(source)
        .set(source.last_synced_on, source.modified)
        .where(source.name.isin(source_names))
    ).run()


def get_external_sync_info(source_doctype: str):
    # 🟢 1. The Mappings Dictionary (Metadata only)
    mappings = {
        "External Customer": {
            "target": "Customer",
            "title_field": "customer_name",
            "force_id": True, # Flag to trigger the forced remote_id naming
            "defaults": {
                "customer_group": "All Customer Groups", 
                "territory": "All Territories", 
                "customer_type": "Company"
            },
        },
        "External Supplier": {
            "target": "Supplier",
            "title_field": "supplier_name",
            "force_id": True,
            "defaults": {"supplier_group": "All Supplier Groups"},
        },
        "External Purchase Invoice": {
            "target": "Purchase Invoice",
            "title_field": "supplier_name",
            "force_id": True,
            "defaults": {},
        },
        "External Payment Entry": {
            "target": "Payment Entry",
            "title_field": "party_name",
            "force_id": True,
            "defaults": {},
        },

        "External Item": {"target": "Item", "title_field": "item_name", "force_id": True, "defaults": {}},
        "External Item Tax Template": {"target": "Item Tax Template", "title_field": "item_tax_template", "force_id": True, "defaults": {}},
        "External Item Group": {"target": "Item Group", "title_field": "item_group_name", "force_id": True, "defaults": {}},
        "External Warehouse": {"target": "Warehouse", "title_field": "warehouse_name", "force_id": True, "defaults": {}},
        "External Cost Center": {"target": "Cost Center", "title_field": "cost_center_name", "force_id": True, "defaults": {}},
        "External Account": {"target": "Account", "title_field": "account_name", "force_id": True, "defaults": {}},
        "External Project Type": {"target": "Project Type", "title_field": "project_type", "force_id": True, "defaults": {}},
        "External Price List": {"target": "Price List", "title_field": "price_list_name", "force_id": True, "defaults": {}},
        "External Contract": {"target": "Contract", "title_field": "contract_name", "force_id": True, "defaults": {}},
        "External Manufacturer": {"target": "Manufacturer", "title_field": "manufacturer_name", "force_id": True, "defaults": {}},
        "External UOM": {"target": "UOM", "title_field": "uom_name", "force_id": True, "defaults": {}},
        "External Uom": {"target": "UOM", "title_field": "uom_name", "force_id": True, "defaults": {}},
        "External Territory": {"target": "Territory", "title_field": "territory_name", "force_id": True, "defaults": {}},
        "External Supplier Group": {"target": "Supplier Group", "title_field": "supplier_group_name", "force_id": True, "defaults": {}},
        "External Customer Group": {"target": "Customer Group", "title_field": "customer_group_name", "force_id": True, "defaults": {}},
        "External Asset Category": {"target": "Asset Category", "title_field": "asset_category_name", "force_id": True, "defaults": {}},
        "External Assets": {"target": "Asset", "title_field": "asset_name", "force_id": True, "defaults": {}},
        "External Location": {"target": "Location", "title_field": "location_name", "force_id": True, "defaults": {}},
        "External Bank": {"target": "Bank", "title_field": "bank_name", "force_id": True, "defaults": {}},
        "External Bank Account": {"target": "Bank Account", "title_field": "bank_account_name", "force_id": True, "defaults": {}},
        "External Expense Claim": {"target": "Expense Claim", "title_field": "employee_name", "force_id": False, "defaults": {}},
        "External Address": {"target": "Address", "title_field": "address_title", "force_id": True, "defaults": {}},
        "External Contact": {"target": "Contact", "title_field": "first_name", "force_id": True, "defaults": {}},
        "External Sales Stage":{"target":"Sales Stage","title_field":"stage_name","force_id":True,"defaults":{}},
        "External Lead Source":{"target":"Lead Source","title_field":"source_name","force_id":True,"defaults":{}},
        "External Work Order":{"target":"Work Order","title_field":"source_name","force_id":True,"defaults":{}},
        "External BOM":{"target":"BOM","title_field":"source_name","force_id":True,"defaults":{}},
        "External Supplier Quotation": {"target": "Supplier Quotation","title_field": "supplier_name","force_id": True,"defaults": {}}

    }

    # 🟢 2. Return from manual mappings
    if source_doctype in mappings:
        return mappings[source_doctype]

    # 🟢 3. Dynamic Fallback logic
    if source_doctype.startswith("External "):
        target = source_doctype.replace("External ", "")
        if target == "Assets": target = "Asset"
        if target == "Uom": target = "UOM"

        target_snake = frappe.scrub(target)
        title_field = None
        meta = frappe.get_meta(source_doctype)
        
        if meta.has_field(f"{target_snake}_name"):
            title_field = f"{target_snake}_name"
        elif meta.has_field("name1"):
            title_field = "name1"

        return {
            "target": target, 
            "title_field": title_field, 
            "defaults": {}, 
            "force_id": True
        }

    return None

def resolve_link_value(doctype: str, value: str, company_abbr: str | None = None) -> str:
    if not value:
        return None
    if doctype in COMPANY_PREFIXED_LINKS:
        prefixed_value = _build_company_prefixed_name(company_abbr, value)
        if prefixed_value and frappe.db.exists(doctype, prefixed_value):
            return prefixed_value

    if frappe.db.exists(doctype, value):
        return value

    if doctype == "External Site Configuration":
        clean_url = str(value).strip().rstrip("/")
        if not clean_url:
            return value

        name = frappe.db.get_value(
            "External Site Configuration",
            {"site_url": ["like", f"{clean_url}%"]},
            "name",
        )
        if name:
            return name

    if doctype in ["Industry", "Industry Type", "Market Segment", "Customer Group", "Supplier Group", "Territory"]:
        name = frappe.db.get_value(doctype, {"name": ["like", f"%{value}%"]}, "name")
        if name:
            return name

    if doctype == "User":
        # Remote user emails almost never exist on the target site.
        # Fall back to Administrator so link validation never blocks a sync.
        return "Administrator"

    return value if frappe.db.exists(doctype, value) else None



def _apply_account_sync_rules(src, tgt, key_field: str) -> None:
    parent_external = src.get("parent_account")

    if parent_external:
        parent_name = _resolve_target_account_from_external(parent_external, key_field, tgt.company)
        parent_name = _nearest_group_account(parent_name, tgt.company)

        # ── Auto-sync parent if not yet in ERPNext ──────────────────────────
        if not parent_name and frappe.db.exists("External Account", parent_external):
            _sync_parent_external_account(
                external_name=parent_external,
                key_field=key_field,
                company=tgt.company,
            )
            parent_name = _resolve_target_account_from_external(parent_external, key_field, tgt.company)
            parent_name = _nearest_group_account(parent_name, tgt.company)
        # ────────────────────────────────────────────────────────────────────

        if parent_name:
            tgt.parent_account = parent_name
            parent_root_type = frappe.db.get_value("Account", parent_name, "root_type")
            parent_report_type = frappe.db.get_value("Account", parent_name, "report_type")
            tgt.root_type = parent_root_type
            tgt.report_type = parent_report_type
            return

        # Fallback: if parent can't be resolved, attach to company root by root_type.
        root_type = (
            tgt.get("root_type")
            or src.get("root_type")
            or _infer_account_root_type(src=src, tgt=tgt)
        )
        company_root_parent = _resolve_company_parent_for_root_account(
            company=tgt.get("company") or src.get("company"),
            root_type=root_type,
        )
        if company_root_parent:
            tgt.parent_account = company_root_parent
            if not tgt.get("root_type"):
                tgt.root_type = root_type
            if not tgt.get("report_type") and root_type:
                tgt.report_type = (
                    "Balance Sheet" if root_type in ("Asset", "Liability", "Equity") else "Profit and Loss"
                )
            return

        if not tgt.get("parent_account"):
            frappe.log_error(
                f"Root account auto-attached without parent. External: {src.name}",
                "External Account Sync Warning"
            )

    root_type = (
        tgt.get("root_type")
        or src.get("root_type")
        or _infer_account_root_type(src=src, tgt=tgt)
    )

    if not root_type:
        frappe.throw(f"Root Type required for External Account: {src.name}")

    # Try getting existing ERPNext root
    existing_root = frappe.db.get_value(
        "Account",
        {
            "company": tgt.company,
            "root_type": root_type,
            "is_group": 1,
            "parent_account": ["is", "not set"],
        },
        "name",
    )

    if existing_root:
        # Attach this external root under ERPNext root
        tgt.parent_account = existing_root
        tgt.root_type = root_type
        tgt.report_type = (
            "Balance Sheet"
            if root_type in ("Asset", "Liability", "Equity")
            else "Profit and Loss"
        )
        tgt.is_group = 1
        return

    # If no ERPNext root found → serious configuration issue
    frappe.throw(
        f"No root account found in company {tgt.company} for root_type {root_type}. "
        "Please recreate Chart of Accounts."
    )

def _apply_bank_account_sync_rules(src, tgt, key_field: str):
    # Add bypass flags
    
    if not tgt.get("account"):
        external_account = src.get("account")

        if external_account:
            resolved = _resolve_target_account_from_external(
                external_account,
                key_field,
                tgt.company
            )
            if resolved:
                tgt.account = resolved


def _apply_expense_claim_sync_rules(src, tgt):
    # Clear link fields that may not exist in target instance
    tgt.cost_center = None
    tgt.project = None               

def _infer_account_root_type(src, tgt) -> str | None:
    account_type = (src.get("account_type") or tgt.get("account_type") or "").strip()
    report_type = (src.get("report_type") or tgt.get("report_type") or "").strip()
    text = " ".join(
        [
            str(src.get("account_name") or ""),
            str(src.get("name") or ""),
            str(account_type or ""),
        ]
    ).lower()

    by_account_type = {
        "Accumulated Depreciation": "Asset",
        "Asset Received But Not Billed": "Asset",
        "Bank": "Asset",
        "Cash": "Asset",
        "Capital Work in Progress": "Asset",
        "Current Asset": "Asset",
        "Depreciation": "Expense",
        "Direct Expense": "Expense",
        "Direct Income": "Income",
        "Equity": "Equity",
        "Expense Account": "Expense",
        "Fixed Asset": "Asset",
        "Income Account": "Income",
        "Indirect Expense": "Expense",
        "Indirect Income": "Income", 
        "Liability": "Liability",
        "Payable": "Liability",
        "Receivable": "Asset",
        "Stock": "Asset",
        "Tax": "Liability",
    }
    if account_type in by_account_type:
        return by_account_type[account_type]

    keyword_map = {
        "income": "Income",
        "expense": "Expense",
        "liability": "Liability",
        "equity": "Equity",
        "asset": "Asset",
    }
    for keyword, inferred in keyword_map.items():
        if keyword in text:
            return inferred

    if report_type == "Profit and Loss":
        return "Income" if "income" in text else "Expense"

    if report_type == "Balance Sheet":
        if "liabil" in text:
            return "Liability"
        if "equit" in text:
            return "Equity"
        if "asset" in text:
            return "Asset"

    return None


def _nearest_group_account(account_name: str | None, company: str | None) -> str | None:
    """Return nearest group account at/above account_name within same company."""
    if not account_name:
        return None

    visited = set()
    current = account_name
    max_depth = 50

    while current and current not in visited and max_depth > 0:
        visited.add(current)
        max_depth -= 1

        row = frappe.db.get_value(
            "Account",
            current,
            ["name", "is_group", "parent_account", "company"],
            as_dict=True,
        )
        if not row:
            return None

        if company and row.get("company") and row.company != company:
            return None

        if frappe.utils.cint(row.get("is_group")):
            return row.get("name")

        current = row.get("parent_account")

    return None


def _resolve_company_parent_for_root_account(company: str | None, root_type: str) -> str | None:
    if not company or not root_type:
        return None

    # 1️⃣ First try: Any top-level group with matching root_type
    root = frappe.get_all(
        "Account",
        filters={
            "company": company,
            "root_type": root_type,
            "is_group": 1,
            "parent_account": ["is", "not set"],
        },
        fields=["name"],
        order_by="lft asc",
        limit_page_length=1,
    )

    if root:
        return root[0]["name"]

    # 2️⃣ Fallback: any group with matching root_type
    root = frappe.get_all(
        "Account",
        filters={
            "company": company,
            "root_type": root_type,
            "is_group": 1,
        },
        fields=["name"],
        order_by="lft asc",
        limit_page_length=1,
    )

    if root:
        return root[0]["name"]

    return None
def _resolve_target_account_from_external(
    external_account_name: str,
    key_field: str,
    company: str,
) -> str | None:
    """
    Resolve an Account.name in target company from an External Account record.
    Fully company-safe. No cross-company contamination.
    """

    if not external_account_name:
        return None

    external_account_name = str(external_account_name).strip()

    # ---------------------------------------------------------
    # 1️⃣ If an Account with this exact name exists in this company, return it
    # ---------------------------------------------------------
    existing = frappe.db.get_value(
        "Account",
        {
            "name": external_account_name,
            "company": company,
        },
        "name",
    )

    if existing:
        return existing

    # ---------------------------------------------------------
    # 2️⃣ Get External Account document
    # ---------------------------------------------------------
    external_doc = frappe.db.get_value(
        "External Account",
        {"name": external_account_name},
        ["name", "remote_id", "parent_account", "account_number", "account_name"],
        as_dict=True,
    )
    if not external_doc:
        external_doc = frappe.db.get_value(
            "External Account",
            {"remote_id": external_account_name},
            ["name", "remote_id", "parent_account", "account_number", "account_name"],
            as_dict=True,
        )

    if not external_doc:
        account_number = _extract_account_number(external_account_name)
        if account_number:
            by_number = frappe.db.get_value(
                "Account",
                {"account_number": account_number, "company": company},
                "name",
            )
            if by_number:
                return by_number
        return None

    remote_id = external_doc.get("remote_id")
    parent_external = external_doc.get("parent_account")

    # ---------------------------------------------------------
    # 3️⃣ Try resolving Account using remote_id + company
    # ---------------------------------------------------------
    target_key_field = _get_actual_target_key("Account", key_field)

    if remote_id and target_key_field:
        account_name = frappe.db.get_value(
            "Account",
            {
                target_key_field: remote_id,
                "company": company,
            },
            "name",
        )
        if account_name:
            return account_name

    # ---------------------------------------------------------
    # 4️⃣ If not found, try resolving by name + company
    # ---------------------------------------------------------
    account_name = frappe.db.get_value(
        "Account",
        {
            "name": remote_id,
            "company": company,
        },
        "name",
    )

    if account_name:
        return account_name

    # Extra fallback: try resolving by account_number directly in name
    account_number = _extract_account_number(external_account_name)
    if account_number:
        name = frappe.db.get_value(
            "Account",
            {"account_number": account_number, "company": company},
            "name",
        )
        if name:
            return name

    account_title = external_doc.get("account_name")
    if account_title:
        account_name = frappe.db.get_value(
            "Account",
            {"account_name": account_title, "company": company},
            "name",
        )
        if account_name:
            return account_name

    # ---------------------------------------------------------
    # 5️⃣ Recursive parent resolution (if needed)
    # ---------------------------------------------------------
    if parent_external:
        return _resolve_target_account_from_external(
            parent_external,
            key_field,
            company,
        )

    return None


def _extract_account_number(account_ref: str | None) -> str | None:
    if not account_ref:
        return None

    raw = str(account_ref).strip()
    if not raw:
        return None

    first = raw.split(" - ", 1)[0].strip()
    return first if first.isdigit() else None


def _get_actual_target_key(target_doctype: str, key_field: str) -> str | None:
    target_fields = {df.fieldname for df in frappe.get_meta(target_doctype).fields}

    if key_field in target_fields:
        return key_field
    if frappe.db.has_column(target_doctype, key_field):
        return key_field
    if f"custom_{key_field}" in target_fields:
        return f"custom_{key_field}"
    if frappe.db.has_column(target_doctype, f"custom_{key_field}"):
        return f"custom_{key_field}"

    return None



# def _apply_asset_sync_rules(src, tgt, key_field: str) -> None:
#     # 1. Item Resolution
#     if not tgt.get("item_code"):
#         resolved_item = _resolve_target_item_from_external(src.get("item_code"), key_field)
#         if not resolved_item:
#             candidate_name = src.get("item_name") or src.get("asset_name")
#             if candidate_name:
#                 resolved_item = frappe.db.get_value("Item", {"item_name": candidate_name, "is_fixed_asset": 1}, "name")
#         tgt.item_code = resolved_item or _create_fixed_asset_item_for_external_asset(src=src, key_field=key_field)

#     # 2. DYNAMIC FIELD CHECK: This stops the 'Unknown column' crash
#     if not tgt.get("location"):
#         valid_company_fields = frappe.get_meta("Company").get_valid_columns()
#         company_loc = None
        
#         # Check for v15 vs older ERPNext field names
#         if "asset_location" in valid_company_fields:
#             company_loc = frappe.db.get_value("Company", tgt.company, "asset_location")
#         elif "location" in valid_company_fields:
#             company_loc = frappe.db.get_value("Company", tgt.company, "location")
            
#         tgt.location = src.get("location") or company_loc or "Store"

#     # 3. Precision Bypass (Rounding to 2 decimals)
#     if src.get("value_after_depreciation"):
#         tgt.value_after_depreciation = flt(src.get("value_after_depreciation"), 2)
    
#     if src.get("gross_purchase_amount"):
#         tgt.gross_purchase_amount = flt(src.get("gross_purchase_amount"), 2)

#     # 4. Docstatus & Submission Flag
#     tgt.docstatus = cint(src.get("docstatus"))
#     if cint(tgt.get("calculate_depreciation")) and not tgt.get("finance_books"):
#         tgt.calculate_depreciation = 0


def _apply_asset_sync_rules(src, tgt, key_field: str) -> None:
    # 1. Item Resolution
    if not tgt.get("item_code"):
        resolved_item = _resolve_target_item_from_external(src.get("item_code"), key_field)
        if not resolved_item:
            candidate_name = src.get("item_name") or src.get("asset_name")
            if candidate_name:
                resolved_item = frappe.db.get_value(
                    "Item", {"item_name": candidate_name, "is_fixed_asset": 1}, "name"
                )
        tgt.item_code = resolved_item or _create_fixed_asset_item_for_external_asset(
            src=src, key_field=key_field
        )

    # 2. Location
    if not tgt.get("location"):
        valid_company_fields = frappe.get_meta("Company").get_valid_columns()
        company_loc = None
        if "asset_location" in valid_company_fields:
            company_loc = frappe.db.get_value("Company", tgt.company, "asset_location")
        elif "location" in valid_company_fields:
            company_loc = frappe.db.get_value("Company", tgt.company, "location")
        tgt.location = src.get("location") or company_loc or "Head Office"

    # 3. FIX: Always set value_after_depreciation — even when it is 0
    raw_vad = src.get("value_after_depreciation")
    if raw_vad is not None:
        tgt.value_after_depreciation = flt(raw_vad, 2)

    if src.get("gross_purchase_amount"):
        tgt.gross_purchase_amount = flt(src.get("gross_purchase_amount"), 2)

    # 4. FIX: Sync finance_books child table for Asset BEFORE depreciation check
    target_meta = frappe.get_meta("Asset")
    _sync_child_tables(src, tgt, target_meta, company_abbr=_get_source_company_abbr(src))

    # 5. FIX: Only disable calculate_depreciation if finance_books is STILL empty
    #    after the child table sync above
    if cint(src.get("calculate_depreciation")):
        if tgt.get("finance_books"):
            tgt.calculate_depreciation = 1
        else:
            # Finance books missing — safer to disable than crash
            tgt.calculate_depreciation = 0
            frappe.log_error(
                f"Asset {src.name}: calculate_depreciation disabled — no finance_books synced",
                "Asset Sync Warning"
            )
    else:
        tgt.calculate_depreciation = 0

    # Docstatus is handled by _save_and_sync_docstatus through ERPNext lifecycle.


def _save_and_sync_docstatus(doc, target_docstatus: int) -> None:
    if target_docstatus not in (0, 1, 2):
        frappe.throw(f"Unsupported docstatus {target_docstatus} for {doc.doctype} {doc.name or ''}")

    doc.flags.ignore_links = True

    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        current_docstatus = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
        if current_docstatus != 0:
            if current_docstatus != target_docstatus:
                frappe.throw(
                    f"{doc.doctype} {doc.name} is submitted/cancelled and cannot be updated in place. "
                    "Cancel and amend it through ERPNext lifecycle before syncing changed source data."
                )
        else:
            doc.save(ignore_permissions=True)

    doc.reload()
    current_docstatus = cint(doc.docstatus)

    if current_docstatus == target_docstatus:
        return

    if target_docstatus == 1 and current_docstatus == 0:
        doc.submit()
    elif target_docstatus == 2:
        if current_docstatus == 0:
            doc.submit()
            doc.reload()
        if cint(doc.docstatus) == 1:
            doc.cancel()
        else:
            frappe.throw(
                f"Cannot cancel {doc.doctype} {doc.name} from docstatus {doc.docstatus}"
            )
    else:
        frappe.throw(
            f"Cannot move {doc.doctype} {doc.name} from docstatus {current_docstatus} "
            f"to {target_docstatus}"
        )

    final_docstatus = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
    if final_docstatus != target_docstatus:
        frappe.throw(
            f"{doc.doctype} {doc.name} lifecycle mismatch. "
            f"Expected docstatus {target_docstatus}, got {final_docstatus}."
        )


def _sanitize_supplier_accounts(tgt) -> None:
    """Remove Supplier.accounts rows that would fail ERPNext's validate_party_accounts.

    ERPNext checks: ``Account.company == SupplierAccount.company`` for every row.
    When syncing from an external site the company or account values may not map
    cleanly, so we drop invalid rows silently instead of aborting the whole sync.
    """
    accounts = tgt.get("accounts") or []
    if not accounts:
        return

    valid_rows = []
    for row in accounts:
        account = row.get("account")
        company = row.get("company")

        if not account or not company:
            # Skip incomplete rows — ERPNext would also reject them.
            continue

        # Resolve company name: use the target-site company if it exists.
        if not frappe.db.exists("Company", company):
            _safe_log_external_sync_error(
                "Supplier accounts row skipped — Company %r not found on target site "
                "(supplier: %s, account: %s)",
                company, tgt.name or "(new)", account,
            )
            continue

        # Validate that the account belongs to the stated company.
        account_company = frappe.db.get_value("Account", account, "company")
        if account_company != company:
            _safe_log_external_sync_error(
                "Supplier accounts row skipped — Account %r belongs to %r, not %r "
                "(supplier: %s)",
                account, account_company, company, tgt.name or "(new)",
            )
            continue

        valid_rows.append(row)

    tgt.set("accounts", valid_rows)


def _apply_supplier_sync_rules(tgt) -> None:
    # Supplier.on_update auto-creates Contact when supplier_primary_contact is empty
    # and mobile/email exists. For long supplier names this can overflow Contact.name.
    # External contacts are synced separately, so avoid implicit contact creation here.
    if not tgt.get("supplier_primary_contact"):
        tgt.mobile_no = None
        tgt.email_id = None

    # Clear portal_users so ERPNext's add_role_for_user() validate hook never calls
    # frappe.get_doc("User", email) with remote-site emails that don't exist locally.
    # Portal users are site-specific and must not be replicated during data sync.
    tgt.set("portal_users", [])

    # Validate the accounts child table: ERPNext's validate_party_accounts checks that
    # each account row's `account` field belongs to the company in the same row.
    # Because External Supplier accounts may reference accounts or companies that don't
    # map cleanly to the target site, we drop any invalid rows rather than failing the
    # entire sync. Valid rows are kept as-is.
    _sanitize_supplier_accounts(tgt)


def _resolve_target_item_from_external(external_item_name: str, key_field: str) -> str | None:
    if not external_item_name:
        return None

    if frappe.db.exists("Item", external_item_name):
        return external_item_name

    target_key_field = _get_actual_target_key("Item", key_field)
    if target_key_field:
        item_name = frappe.db.get_value("Item", {target_key_field: external_item_name}, "name")
        if item_name:
            return item_name

    if not frappe.db.exists("External Item", external_item_name):
        return None

    parent_remote_id = frappe.db.get_value("External Item", external_item_name, key_field)
    if target_key_field and parent_remote_id:
        item_name = frappe.db.get_value("Item", {target_key_field: parent_remote_id}, "name")
        if item_name:
            return item_name

    try:
        sync_doc_by_key(
            source_doctype="External Item",
            source_name=external_item_name,
            target_doctype="Item",
            key_field=key_field,
            title_field_source="item_name",
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"External Item auto-sync failed while resolving item for External Asset (external item: {external_item_name})",
        )
        return None

    if target_key_field and parent_remote_id:
        return frappe.db.get_value("Item", {target_key_field: parent_remote_id}, "name")

    return frappe.db.get_value("Item", {"name": external_item_name}, "name")


def _create_fixed_asset_item_for_external_asset(src, key_field: str) -> str | None:
    item_code = (src.get("item_code") or "").strip()
    if not item_code:
        item_code = f"EXT-FA-{src.get(key_field) or src.name}"

    if frappe.db.exists("Item", item_code):
        return item_code

    item_name = (src.get("item_name") or src.get("asset_name") or item_code).strip()
    asset_category = src.get("asset_category")
    if asset_category:
        asset_category = _resolve_target_asset_category_from_external(asset_category, key_field) or asset_category
    if not asset_category or not frappe.db.exists("Asset Category", asset_category):
        asset_category = frappe.db.get_value("Asset Category", {}, "name")
    if not asset_category:
        return None

    company_abbr = _get_source_company_abbr(src)
    item_group = resolve_link_value("Item Group", "All Item Groups", company_abbr=company_abbr)
    if not item_group:
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    if not item_group:
        return None

    stock_uom = "Nos" if frappe.db.exists("UOM", "Nos") else None
    if not stock_uom:
        stock_uom = frappe.db.get_value("UOM", {}, "name")
    if not stock_uom:
        return None

    item = frappe.new_doc("Item")
    item.item_code = item_code
    item.item_name = item_name
    item.item_group = item_group
    item.stock_uom = stock_uom
    item.is_stock_item = 0
    item.is_fixed_asset = 1
    item.asset_category = asset_category

    target_key_field = _get_actual_target_key("Item", key_field)
    if target_key_field and src.get(key_field):
        item.set(target_key_field, src.get(key_field))

    if hasattr(item, "source_site") and src.get("source_site"):
        item.source_site = src.get("source_site")

    item.insert(ignore_permissions=True)
    return item.name


def _resolve_target_asset_category_from_external(external_asset_category: str, key_field: str) -> str | None:
    if not external_asset_category:
        return None

    if frappe.db.exists("Asset Category", external_asset_category):
        return external_asset_category

    target_key_field = _get_actual_target_key("Asset Category", key_field)
    if target_key_field:
        category_name = frappe.db.get_value("Asset Category", {target_key_field: external_asset_category}, "name")
        if category_name:
            return category_name

    if not frappe.db.exists("External Asset Category", external_asset_category):
        return None

    category_remote_id = frappe.db.get_value("External Asset Category", external_asset_category, key_field)
    if target_key_field and category_remote_id:
        category_name = frappe.db.get_value("Asset Category", {target_key_field: category_remote_id}, "name")
        if category_name:
            return category_name

    try:
        sync_doc_by_key(
            source_doctype="External Asset Category",
            source_name=external_asset_category,
            target_doctype="Asset Category",
            key_field=key_field,
            title_field_source="asset_category_name",
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "External Asset Category auto-sync failed while resolving asset category for External Asset",
        )
        return None

    if frappe.db.exists("Asset Category", external_asset_category):
        return external_asset_category
    if target_key_field and category_remote_id:
        return frappe.db.get_value("Asset Category", {target_key_field: category_remote_id}, "name")
    return None


def _apply_item_group_sync_rules(
    src,
    tgt,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> None:
    _sanitize_item_group_default_accounts(src, tgt)

    # Avoid nested-set reparenting issues on existing nodes; keep current tree
    # stable and only set parent when creating a new Item Group.

    # ═══════════════════════════════════════════════════════════════════════
    # FIX: Prevent demoting a group node to leaf if it has children
    # This avoids: "Item Group X cannot be a leaf node as it has children"
    # ═══════════════════════════════════════════════════════════════════════
    if not tgt.is_new():
        has_item_group_children = frappe.db.exists(
            "Item Group", {"parent_item_group": tgt.name}
        )
        has_item_children = frappe.db.exists(
            "Item", {"item_group": tgt.name}
        )

        if (has_item_group_children or has_item_children) and src.get("is_group") == 0:
            tgt.is_group = 1
            _safe_log(
                "warning",
                f"Item Group {tgt.name}: preserved is_group=1 because it has "
                f"children (item_groups={bool(has_item_group_children)}, "
                f"items={bool(has_item_children)}). Source had is_group=0."
            )
    # ═══════════════════════════════════════════════════════════════════════

    if not tgt.is_new():
        db_parent = tgt.get("parent_item_group")
        if hasattr(tgt, "old_parent"):
            tgt.old_parent = db_parent
        return

    parent_external = src.get("parent_item_group")
    if not parent_external:
        if hasattr(tgt, "old_parent"):
            tgt.old_parent = tgt.get("parent_item_group")
        return

    self_markers = {
        src.name,
        src.get(key_field),
        src.get(title_field_source) if title_field_source else None,
        tgt.name,
    }
    if parent_external in {v for v in self_markers if v}:
        return

    parent_name = _resolve_target_item_group_from_external(
        external_item_group_name=parent_external,
        key_field=key_field,
        title_field_source=title_field_source,
        company_abbr=company_abbr,
    )
    if not parent_name:
        return

    if tgt.name and parent_name == tgt.name:
        return

    if tgt.name and frappe.db.exists("Item Group", tgt.name):
        if _is_nestedset_descendant("Item Group", parent_name, tgt.name):
            return

    tgt.parent_item_group = parent_name


def _sanitize_item_group_default_accounts(src, tgt) -> None:
    if not tgt.get("item_group_defaults"):
        return

    company = _get_source_configuration_company(src) or _get_single_child_value(
        tgt.get("item_group_defaults"), "company"
    )
    if not company:
        return

    valid_rows = []
    skipped = []
    for row in tgt.get("item_group_defaults"):
        expense_account = row.get("expense_account")
        if expense_account and not _account_belongs_to_company(expense_account, company):
            skipped.append(
                f"{tgt.name or src.name}: dropped Item Default expense account "
                f"{expense_account} for company {company}"
            )
            continue
        valid_rows.append(row)

    if len(valid_rows) == len(tgt.get("item_group_defaults")):
        return

    tgt.set("item_group_defaults", [])
    for row in valid_rows:
        row_data = row.as_dict() if hasattr(row, "as_dict") else dict(row)
        tgt.append(
            "item_group_defaults",
            {key: value for key, value in row_data.items() if key not in SYSTEM_FIELDS},
        )

    for message in skipped:
        _safe_log_error(message=message, title="External Item Group Sync Warning")


def _get_source_configuration_company(src) -> str | None:
    sap_configuration = src.get("external_sap_configuration")
    if sap_configuration:
        return frappe.db.get_value(
            "External SAP Configuration",
            sap_configuration,
            "company",
        )

    source_site = src.get("source_site")
    if not source_site:
        return None

    return frappe.db.get_value("External Site Configuration", source_site, "company")


def _get_source_company_abbr(src) -> str | None:
    company = _get_source_configuration_company(src) or src.get("company")
    if not company:
        return None

    return frappe.db.get_value("Company", company, "abbr")


def _build_company_prefixed_name(company_abbr: str | None, name: str | None) -> str | None:
    if not name:
        return None

    name = str(name).strip()
    if not name or not company_abbr:
        return name

    prefix = f"{company_abbr}-"
    if name.startswith(prefix):
        return name

    return f"{prefix}{name}"


def _matches_company_prefixed_name(name: str | None, company_abbr: str | None) -> bool:
    if not name or not company_abbr:
        return True

    return str(name).startswith(f"{company_abbr}-")


def _resolve_mandatory_defaults(target_doctype: str, defaults: dict | None, company_abbr: str | None) -> dict:
    resolved = dict(defaults or {})
    default_link_fields = {
        "Customer": {"customer_group": "Customer Group"},
        "Supplier": {"supplier_group": "Supplier Group"},
        "Item": {"item_group": "Item Group"},
    }

    for fieldname, link_doctype in default_link_fields.get(target_doctype, {}).items():
        if resolved.get(fieldname):
            resolved[fieldname] = resolve_link_value(
                link_doctype,
                resolved[fieldname],
                company_abbr=company_abbr,
            ) or resolved[fieldname]

    return resolved


def _get_single_child_value(rows, fieldname: str):
    values = {row.get(fieldname) for row in rows if row.get(fieldname)}
    return values.pop() if len(values) == 1 else None


def _account_belongs_to_company(account: str, company: str) -> bool:
    return bool(
        frappe.db.exists(
            "Account",
            {
                "name": account,
                "company": company,
            },
        )
    )

def _resolve_target_item_group_from_external(
    external_item_group_name: str,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> str | None:
    if not external_item_group_name:
        return None

    # Direct name match
    if frappe.db.exists("Item Group", external_item_group_name):
        return external_item_group_name

    # Match by key field (remote_id)
    target_key_field = _get_actual_target_key("Item Group", key_field)
    if target_key_field:
        name = frappe.db.get_value("Item Group", {target_key_field: external_item_group_name}, "name")
        if name:
            return name

    # Parent is commonly an External Item Group row name.
    if not frappe.db.exists("External Item Group", external_item_group_name):
        return None

    parent_remote_id = frappe.db.get_value("External Item Group", external_item_group_name, key_field)
    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Item Group", {target_key_field: parent_remote_id}, "name")
        if name:
            return name

    # Match by item_group_name (title field)
    if title_field_source:
        parent_title = frappe.db.get_value("External Item Group", external_item_group_name, title_field_source)
        if parent_title and frappe.db.exists("Item Group", parent_title):
            return parent_title

    # On-demand parent sync if still unresolved.
    raw_stack = getattr(frappe.flags, "external_item_group_sync_stack", None)
    if raw_stack is None:
        stack = []
    elif isinstance(raw_stack, (set, list, tuple)):
        stack = list(raw_stack)
    else:
        stack = [raw_stack]

    if external_item_group_name in stack:
        return None

    stack.append(external_item_group_name)
    frappe.flags.external_item_group_sync_stack = stack
    try:
        sync_doc_by_key(
            source_doctype="External Item Group",
            source_name=external_item_group_name,
            target_doctype="Item Group",
            key_field=key_field,
            title_field_source=title_field_source,
        )
    finally:
        if external_item_group_name in stack:
            stack.remove(external_item_group_name)

    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Item Group", {target_key_field: parent_remote_id}, "name")
        if name:
            return name

    if title_field_source:
        parent_title = frappe.db.get_value("External Item Group", external_item_group_name, title_field_source)
        if parent_title and frappe.db.exists("Item Group", parent_title):
            return parent_title

    return None


def _is_nestedset_descendant(doctype: str, node_name: str, ancestor_name: str) -> bool:
    node_bounds = frappe.db.get_value(doctype, node_name, ["lft", "rgt"], as_dict=True)
    ancestor_bounds = frappe.db.get_value(doctype, ancestor_name, ["lft", "rgt"], as_dict=True)
    if not node_bounds or not ancestor_bounds:
        return False

    return (
        node_bounds.lft is not None
        and node_bounds.rgt is not None
        and ancestor_bounds.lft is not None
        and ancestor_bounds.rgt is not None
        and node_bounds.lft > ancestor_bounds.lft
        and node_bounds.rgt < ancestor_bounds.rgt
    )


def _is_root_item_group_doc(tgt, src, title_field_source: str | None) -> bool:
    candidates = {
        tgt.name,
        src.name,
        src.get(title_field_source) if title_field_source else None,
    }
    return "All Item Groups" in {v for v in candidates if v}


def _apply_supplier_group_sync_rules(
    src,
    tgt,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> None:
    """
    Apply supplier group specific sync rules with loop prevention.
    """
    # Add flags to bypass validation during sync
    
    # ERPNext disallows leaf nodes that already have children.
    if _supplier_group_has_children(src=src, tgt=tgt, key_field=key_field, title_field_source=title_field_source):
        tgt.is_group = 1

    # For existing documents, don't modify parent during sync if it already has one
    if not tgt.is_new():
        db_parent = frappe.db.get_value("Supplier Group", tgt.name, "parent_supplier_group")
        if db_parent and _matches_company_prefixed_name(db_parent, company_abbr):
            _safe_log("debug", f"Supplier Group {tgt.name} already exists with parent {db_parent}, skipping parent assignment")
            tgt.parent_supplier_group = db_parent
            if hasattr(tgt, "old_parent"):
                tgt.old_parent = db_parent
            return

    # For new documents, set parent carefully
    parent_external = src.get("parent_supplier_group")
    if not parent_external:
        _safe_log("debug", f"No parent specified for {src.name}")
        return

    # Get all possible identifiers for this node to prevent self-reference
    self_identifiers = {
        str(v) for v in [
            src.name,
            src.get(key_field),
            src.get(title_field_source) if title_field_source else None,
            tgt.name,
            src.get("supplier_group_name"),
            _build_company_prefixed_name(company_abbr, src.get(key_field)),
            _build_company_prefixed_name(
                company_abbr,
                src.get(title_field_source) if title_field_source else None,
            ),
        ] if v
    }
    
    _safe_log("debug", f"Self identifiers for {src.name}: {self_identifiers}")
    
    # Don't set parent if it's self
    if str(parent_external) in self_identifiers:
        _safe_log("warning", f"Prevented self-parent reference for {src.name}")
        return

    # Resolve the parent name
    parent_name = _resolve_target_supplier_group_from_external(
        external_supplier_group_name=parent_external,
        key_field=key_field,
        title_field_source=title_field_source,
        company_abbr=company_abbr,
    )
    
    _safe_log("debug", f"Resolved parent '{parent_external}' to '{parent_name}'")
    
    if not parent_name:
        _safe_log("warning", f"Could not resolve parent '{parent_external}' for {src.name}")
        return

    # Check if parent exists
    if not frappe.db.exists("Supplier Group", parent_name):
        _safe_log("warning", f"Parent '{parent_name}' does not exist in database")
        return

    # For new documents, tgt.name might not be set yet
    # Use the key_value or title as potential name
    potential_name = tgt.name or src.get(key_field) or src.get(title_field_source) or src.name
    
    # CRITICAL CHECK: Verify this wouldn't create a loop
    if frappe.db.exists("Supplier Group", potential_name):
        # Node already exists in DB, check if parent is a descendant
        if _is_descendant("Supplier Group", parent_name, potential_name):
            _safe_log(
                "error",
                f"PREVENTED LOOP: Cannot set parent '{parent_name}' for '{potential_name}' "
                f"because parent is a descendant"
            )
            return
        
        # Also check if parent is the same as node
        if parent_name == potential_name:
            _safe_log("error", f"PREVENTED SELF-PARENT: Cannot set parent to self for '{potential_name}'")
            return
    
    # All checks passed, set the parent
    _safe_log("debug", f"Setting parent for {potential_name} to {parent_name}")
    tgt.parent_supplier_group = parent_name


def _is_descendant(doctype: str, potential_parent: str, node_name: str) -> bool:
    """
    Check if potential_parent is a descendant of node_name.
    Returns True if parent is descendant of node.
    """
    if not potential_parent or not node_name:
        return False
    
    # Get lft and rgt values
    node_bounds = frappe.db.get_value(doctype, node_name, ["lft", "rgt"], as_dict=True)
    parent_bounds = frappe.db.get_value(doctype, potential_parent, ["lft", "rgt"], as_dict=True)
    
    if not node_bounds or not parent_bounds:
        return False
    
    # If parent is within node's range, it's a descendant
    is_descendant = (
        parent_bounds.lft > node_bounds.lft and 
        parent_bounds.rgt < node_bounds.rgt
    )
    
    if is_descendant:
        _safe_log("debug", 
            f"Parent {potential_parent} (lft:{parent_bounds.lft}, rgt:{parent_bounds.rgt}) "
            f"is descendant of {node_name} (lft:{node_bounds.lft}, rgt:{node_bounds.rgt})"
        )
    
    return is_descendant


def _resolve_target_supplier_group_from_external(
    external_supplier_group_name: str,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> str | None:
    """
    Resolve external supplier group reference to actual Supplier Group name with cycle prevention.
    """
    if not external_supplier_group_name:
        return None

    _safe_log("debug", f"Resolving external supplier group: {external_supplier_group_name}")

    # Check stack to prevent infinite recursion
    stack_key = "external_supplier_group_resolution_stack"
    stack = getattr(frappe.flags, stack_key, [])
    if not isinstance(stack, list):
        stack = []
    
    if external_supplier_group_name in stack:
        _safe_log("warning", f"Cycle detected in resolution: {external_supplier_group_name}")
        return None
    
    stack.append(external_supplier_group_name)
    frappe.flags.external_supplier_group_resolution_stack = stack
    
    try:
        prefixed_name = _build_company_prefixed_name(company_abbr, external_supplier_group_name)
        if prefixed_name and frappe.db.exists("Supplier Group", prefixed_name):
            _safe_log("debug", f"Found company-prefixed match: {prefixed_name}")
            return prefixed_name

        if not company_abbr and frappe.db.exists("Supplier Group", external_supplier_group_name):
            _safe_log("debug", f"Found direct match: {external_supplier_group_name}")
            return external_supplier_group_name

        # Try by key field
        target_key_field = _get_actual_target_key("Supplier Group", key_field)
        if target_key_field:
            name = frappe.db.get_value(
                "Supplier Group", 
                {target_key_field: external_supplier_group_name}, 
                "name"
            )
            if name:
                _safe_log("debug", f"Found by key field {target_key_field}: {name}")
                return name

        # Check if it's an External Supplier Group
        if not frappe.db.exists("External Supplier Group", external_supplier_group_name):
            _safe_log("debug", f"Not an External Supplier Group: {external_supplier_group_name}")
            return None

        # Get the external doc
        external_doc = frappe.get_doc("External Supplier Group", external_supplier_group_name)
        
        # Try by remote_id
        remote_id = external_doc.get(key_field)
        if target_key_field and remote_id:
            name = frappe.db.get_value(
                "Supplier Group", 
                {target_key_field: remote_id}, 
                "name"
            )
            if name:
                _safe_log("debug", f"Found by remote_id {remote_id}: {name}")
                return name

        # Try by title
        if title_field_source:
            title = external_doc.get(title_field_source)
            prefixed_title = _build_company_prefixed_name(company_abbr, title)
            if prefixed_title and frappe.db.exists("Supplier Group", prefixed_title):
                _safe_log("debug", f"Found by company-prefixed title {prefixed_title}")
                return prefixed_title
            if not company_abbr and title and frappe.db.exists("Supplier Group", title):
                _safe_log("debug", f"Found by title {title}: {title}")
                return title

        # Don't auto-sync parent during resolution to prevent cycles
        _safe_log("debug", f"Could not resolve {external_supplier_group_name}")
        return None
        
    finally:
        # Remove from stack
        if external_supplier_group_name in stack:
            stack.remove(external_supplier_group_name)
        frappe.flags.external_supplier_group_resolution_stack = stack


def _would_create_loop(doctype: str, node_name: str | None, parent_name: str) -> bool:
    """
    Check if setting parent_name as parent of node_name would create a loop.
    Returns True if parent_name is a descendant of node_name.
    """
    if not node_name or not parent_name:
        return False
    
    # If node doesn't exist yet, it can't have descendants
    if not frappe.db.exists(doctype, node_name):
        return False
    
    # Get lft and rgt values
    node_bounds = frappe.db.get_value(doctype, node_name, ["lft", "rgt"], as_dict=True)
    parent_bounds = frappe.db.get_value(doctype, parent_name, ["lft", "rgt"], as_dict=True)
    
    if not node_bounds or not parent_bounds:
        return False
    
    # Check if parent is within node's range (meaning parent is descendant of node)
    return (parent_bounds.lft > node_bounds.lft and parent_bounds.rgt < node_bounds.rgt)


def _resolve_target_supplier_group_from_external(
    external_supplier_group_name: str,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> str | None:
    if not external_supplier_group_name:
        return None

    prefixed_name = _build_company_prefixed_name(company_abbr, external_supplier_group_name)
    if prefixed_name and frappe.db.exists("Supplier Group", prefixed_name):
        return prefixed_name

    if not company_abbr and frappe.db.exists("Supplier Group", external_supplier_group_name):
        return external_supplier_group_name

    target_key_field = _get_actual_target_key("Supplier Group", key_field)
    if target_key_field:
        name = frappe.db.get_value("Supplier Group", {target_key_field: external_supplier_group_name}, "name")
        if name and _matches_company_prefixed_name(name, company_abbr):
            return name

    if not frappe.db.exists("External Supplier Group", external_supplier_group_name):
        return None

    parent_remote_id = frappe.db.get_value("External Supplier Group", external_supplier_group_name, key_field)
    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Supplier Group", {target_key_field: parent_remote_id}, "name")
        if name and _matches_company_prefixed_name(name, company_abbr):
            return name

    if title_field_source:
        parent_title = frappe.db.get_value(
            "External Supplier Group", external_supplier_group_name, title_field_source
        )
        prefixed_title = _build_company_prefixed_name(company_abbr, parent_title)
        if prefixed_title and frappe.db.exists("Supplier Group", prefixed_title):
            return prefixed_title
        if not company_abbr and parent_title and frappe.db.exists("Supplier Group", parent_title):
            return parent_title

    raw_stack = getattr(frappe.flags, "external_supplier_group_sync_stack", None)
    if raw_stack is None:
        stack = []
    elif isinstance(raw_stack, (set, list, tuple)):
        stack = list(raw_stack)
    else:
        stack = [raw_stack]

    if external_supplier_group_name in stack:
        return None

    stack.append(external_supplier_group_name)
    frappe.flags.external_supplier_group_sync_stack = stack
    try:
        sync_doc_by_key(
            source_doctype="External Supplier Group",
            source_name=external_supplier_group_name,
            target_doctype="Supplier Group",
            key_field=key_field,
            title_field_source=title_field_source,
        )
    finally:
        if external_supplier_group_name in stack:
            stack.remove(external_supplier_group_name)

    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Supplier Group", {target_key_field: parent_remote_id}, "name")
        if name and _matches_company_prefixed_name(name, company_abbr):
            return name

    if title_field_source:
        parent_title = frappe.db.get_value(
            "External Supplier Group", external_supplier_group_name, title_field_source
        )
        prefixed_title = _build_company_prefixed_name(company_abbr, parent_title)
        if prefixed_title and frappe.db.exists("Supplier Group", prefixed_title):
            return prefixed_title
        if not company_abbr and parent_title and frappe.db.exists("Supplier Group", parent_title):
            return parent_title

    return None


def _is_root_supplier_group_doc(tgt, src, title_field_source: str | None) -> bool:
    candidates = {
        tgt.name,
        src.name,
        src.get(title_field_source) if title_field_source else None,
    }
    return "All Supplier Groups" in {v for v in candidates if v}


def _supplier_group_has_children(src, tgt, key_field: str, title_field_source: str | None) -> bool:
    candidate_parents = {v for v in {src.name, tgt.name, src.get(key_field), src.get(title_field_source) if title_field_source else None} if v}

    for parent in candidate_parents:
        if frappe.db.exists("Supplier Group", {"parent_supplier_group": parent}):
            return True

        if frappe.db.exists("External Supplier Group", {"parent_supplier_group": parent}):
            return True

    return False


def _territory_has_children(src, tgt, key_field: str, title_field_source: str | None) -> bool:
    """Return True when the Territory node already has child records in the DB."""
    candidate_parents = {
        v
        for v in {
            src.name,
            tgt.name,
            src.get(key_field),
            src.get(title_field_source) if title_field_source else None,
        }
        if v
    }

    for parent in candidate_parents:
        if frappe.db.exists("Territory", {"parent_territory": parent}):
            return True
        if frappe.db.exists("External Territory", {"parent_territory": parent}):
            return True

    return False


def _location_has_children(src, tgt, key_field: str, title_field_source: str | None) -> bool:
    """Return True when the Location node already has child records in the DB."""
    candidate_parents = {
        v
        for v in {
            src.name,
            tgt.name,
            src.get(key_field),
            src.get(title_field_source) if title_field_source else None,
        }
        if v
    }

    for parent in candidate_parents:
        if frappe.db.exists("Location", {"parent_location": parent}):
            return True
        if frappe.db.exists("External Location", {"parent_location": parent}):
            return True

    return False


def _apply_location_sync_rules(src, tgt, key_field: str, title_field_source: str | None) -> None:
    """
    Apply Location-specific sync rules.

    Mirrors _apply_territory_sync_rules:
    - Force is_group=1 when the node already has DB children.
    - Pin parent_location and old_parent from the DB for existing nodes to
      prevent ERPNext's nested-set validator from raising
      "Item cannot be added to its own descendants".
    - For new nodes, resolve and set parent_location safely.
    """
    ROOT = "All Locations"

    # Ensure the root node exists.
    if not frappe.db.exists("Location", ROOT):
        frappe.get_doc({
            "doctype": "Location",
            "location_name": ROOT,
            "is_group": 1,
        }).insert(ignore_permissions=True)

    # Force is_group when this node already has children in the DB.
    if _location_has_children(src=src, tgt=tgt, key_field=key_field, title_field_source=title_field_source):
        tgt.is_group = 1

    # For existing nodes: read parent from DB and pin both fields so the
    # nested-set validator does not see a spurious parent change.
    if not tgt.is_new():
        db_parent = frappe.db.get_value("Location", tgt.name, "parent_location")
        tgt.parent_location = db_parent
        if hasattr(tgt, "old_parent"):
            tgt.old_parent = db_parent
        _safe_log(
            "debug",
            f"Location {tgt.name} already exists with parent {db_parent}, skipping parent assignment",
        )
        return

    # Root node itself — no parent.
    title_value = src.get(title_field_source) if title_field_source else None
    is_root = (tgt.name == ROOT or title_value == ROOT or src.name == ROOT)
    if is_root:
        tgt.parent_location = None
        tgt.is_group = 1
        return

    parent_external = src.get("parent_location")

    # No parent specified — attach to ROOT.
    if not parent_external:
        tgt.parent_location = ROOT
        return

    # Prevent self-parenting.
    self_markers = {
        src.name,
        src.get(key_field),
        title_value,
        tgt.name,
    }
    if parent_external in {v for v in self_markers if v}:
        tgt.parent_location = ROOT
        return

    # Resolve the parent to an actual Location name.
    parent_name = None
    if frappe.db.exists("Location", parent_external):
        parent_name = parent_external
    else:
        target_key_field = _get_actual_target_key("Location", key_field)
        if target_key_field:
            parent_name = frappe.db.get_value(
                "Location", {target_key_field: parent_external}, "name"
            )
        if not parent_name and title_field_source:
            parent_title = frappe.db.get_value(
                "External Location", parent_external, title_field_source
            ) if frappe.db.exists("External Location", parent_external) else None
            if parent_title and frappe.db.exists("Location", parent_title):
                parent_name = parent_title

    if not parent_name:
        tgt.parent_location = ROOT
        return

    # Prevent self-reference after resolution.
    if tgt.name and parent_name == tgt.name:
        tgt.parent_location = ROOT
        return

    # Prevent moving a node under its own descendant (loop guard).
    if tgt.name and frappe.db.exists("Location", tgt.name):
        node = frappe.db.get_value("Location", tgt.name, ["lft", "rgt"], as_dict=True)
        parent = frappe.db.get_value("Location", parent_name, ["lft", "rgt"], as_dict=True)
        if node and parent and parent.lft > node.lft and parent.rgt < node.rgt:
            _safe_log("warning", f"Prevented Location loop: {tgt.name} -> {parent_name}")
            tgt.parent_location = ROOT
            return

    tgt.parent_location = parent_name


def _apply_customer_group_sync_rules(
    src,
    tgt,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> None:
    if not tgt.is_new():
        db_parent = frappe.db.get_value("Customer Group", tgt.name, "parent_customer_group")
        if db_parent and _matches_company_prefixed_name(db_parent, company_abbr):
            tgt.parent_customer_group = db_parent
            if hasattr(tgt, "old_parent"):
                tgt.old_parent = db_parent
            return

    parent_external = src.get("parent_customer_group")
    if not parent_external:
        return

    self_markers = {
        src.name,
        src.get(key_field),
        src.get(title_field_source) if title_field_source else None,
        tgt.name,
        _build_company_prefixed_name(company_abbr, src.get(key_field)),
        _build_company_prefixed_name(
            company_abbr,
            src.get(title_field_source) if title_field_source else None,
        ),
    }
    if parent_external in {v for v in self_markers if v}:
        return

    parent_name = _resolve_target_customer_group_from_external(
        external_customer_group_name=parent_external,
        key_field=key_field,
        title_field_source=title_field_source,
        company_abbr=company_abbr,
    )
    if not parent_name:
        return

    if tgt.name and parent_name == tgt.name:
        return

    if tgt.name and frappe.db.exists("Customer Group", tgt.name):
        if _is_nestedset_descendant("Customer Group", parent_name, tgt.name):
            return

    tgt.parent_customer_group = parent_name


def _resolve_target_customer_group_from_external(
    external_customer_group_name: str,
    key_field: str,
    title_field_source: str | None,
    company_abbr: str | None = None,
) -> str | None:
    if not external_customer_group_name:
        return None

    prefixed_name = _build_company_prefixed_name(company_abbr, external_customer_group_name)
    if prefixed_name and frappe.db.exists("Customer Group", prefixed_name):
        return prefixed_name

    if not company_abbr and frappe.db.exists("Customer Group", external_customer_group_name):
        return external_customer_group_name

    target_key_field = _get_actual_target_key("Customer Group", key_field)
    if target_key_field:
        name = frappe.db.get_value("Customer Group", {target_key_field: external_customer_group_name}, "name")
        if name and _matches_company_prefixed_name(name, company_abbr):
            return name

    if not frappe.db.exists("External Customer Group", external_customer_group_name):
        return None

    parent_remote_id = frappe.db.get_value("External Customer Group", external_customer_group_name, key_field)
    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Customer Group", {target_key_field: parent_remote_id}, "name")
        if name and _matches_company_prefixed_name(name, company_abbr):
            return name

    if title_field_source:
        parent_title = frappe.db.get_value(
            "External Customer Group", external_customer_group_name, title_field_source
        )
        prefixed_title = _build_company_prefixed_name(company_abbr, parent_title)
        if prefixed_title and frappe.db.exists("Customer Group", prefixed_title):
            return prefixed_title
        if not company_abbr and parent_title and frappe.db.exists("Customer Group", parent_title):
            return parent_title

    raw_stack = getattr(frappe.flags, "external_customer_group_sync_stack", None)
    if raw_stack is None:
        stack = []
    elif isinstance(raw_stack, (set, list, tuple)):
        stack = list(raw_stack)
    else:
        stack = [raw_stack]

    if external_customer_group_name in stack:
        return None

    stack.append(external_customer_group_name)
    frappe.flags.external_customer_group_sync_stack = stack
    try:
        sync_doc_by_key(
            source_doctype="External Customer Group",
            source_name=external_customer_group_name,
            target_doctype="Customer Group",
            key_field=key_field,
            title_field_source=title_field_source,
        )
    finally:
        if external_customer_group_name in stack:
            stack.remove(external_customer_group_name)

    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Customer Group", {target_key_field: parent_remote_id}, "name")
        if name and _matches_company_prefixed_name(name, company_abbr):
            return name

    if title_field_source:
        parent_title = frappe.db.get_value(
            "External Customer Group", external_customer_group_name, title_field_source
        )
        prefixed_title = _build_company_prefixed_name(company_abbr, parent_title)
        if prefixed_title and frappe.db.exists("Customer Group", prefixed_title):
            return prefixed_title
        if not company_abbr and parent_title and frappe.db.exists("Customer Group", parent_title):
            return parent_title

    return None


def _apply_territory_sync_rules(src, tgt, key_field: str, title_field_source: str | None) -> None:

    ROOT = "All Territories"

    # ✅ Ensure ROOT exists
    if not frappe.db.exists("Territory", ROOT):
        frappe.get_doc({
            "doctype": "Territory",
            "territory_name": ROOT,
            "is_group": 1
        }).insert(ignore_permissions=True)

    # ✅ Force is_group when this node already has children in the DB.
    # ERPNext rejects saving a leaf node that has children with:
    # "cannot be a leaf node as it has children".
    if _territory_has_children(src=src, tgt=tgt, key_field=key_field, title_field_source=title_field_source):
        tgt.is_group = 1

    # ✅ Never re-parent existing nodes (VERY IMPORTANT)
    # Read the current parent from the DB (not the in-memory doc) and pin both
    # parent_territory and old_parent to it.  This mirrors the Supplier Group
    # fix and prevents ERPNext's nested-set validator from seeing a spurious
    # parent change that triggers "Item cannot be added to its own descendants".
    if not tgt.is_new():
        db_parent = frappe.db.get_value("Territory", tgt.name, "parent_territory")
        tgt.parent_territory = db_parent
        if hasattr(tgt, "old_parent"):
            tgt.old_parent = db_parent
        _safe_log(
            "debug",
            f"Territory {tgt.name} already exists with parent {db_parent}, skipping parent assignment",
        )
        return

    # ✅ If this is ROOT itself → don’t set parent
    if _is_root_territory_doc(tgt=tgt, src=src, title_field_source=title_field_source):
        tgt.parent_territory = None
        tgt.is_group = 1
        return

    parent_external = src.get("parent_territory")

    # ✅ No parent → attach to ROOT
    if not parent_external:
        tgt.parent_territory = ROOT
        return

    # ✅ Prevent self-parenting
    self_markers = {
        src.name,
        src.get(key_field),
        src.get(title_field_source) if title_field_source else None,
        tgt.name,
    }

    if parent_external in {v for v in self_markers if v}:
        tgt.parent_territory = ROOT
        return

    # ✅ Resolve parent
    parent_name = _resolve_target_territory_from_external(
        external_territory_name=parent_external,
        key_field=key_field,
        title_field_source=title_field_source,
    )

    # ✅ Parent not resolved → attach to ROOT
    if not parent_name:
        tgt.parent_territory = ROOT
        return

    # ✅ Prevent self reference again
    if tgt.name and parent_name == tgt.name:
        tgt.parent_territory = ROOT
        return

    # ✅ MOST IMPORTANT — prevent moving node under its own child
    if tgt.name and frappe.db.exists("Territory", tgt.name):

        node = frappe.db.get_value("Territory", tgt.name, ["lft", "rgt"], as_dict=True)
        parent = frappe.db.get_value("Territory", parent_name, ["lft", "rgt"], as_dict=True)

        if node and parent and parent.lft > node.lft and parent.rgt < node.rgt:
            _safe_log("warning", 
                f"Prevented Territory loop: {tgt.name} -> {parent_name}"
            )
            tgt.parent_territory = ROOT
            return

    tgt.parent_territory = parent_name


def _resolve_target_territory_from_external(
    external_territory_name: str, key_field: str, title_field_source: str | None
) -> str | None:
    if not external_territory_name:
        return None

    if frappe.db.exists("Territory", external_territory_name):
        return external_territory_name

    target_key_field = _get_actual_target_key("Territory", key_field)
    if target_key_field:
        name = frappe.db.get_value("Territory", {target_key_field: external_territory_name}, "name")
        if name:
            return name

    if not frappe.db.exists("External Territory", external_territory_name):
        return None

    parent_remote_id = frappe.db.get_value("External Territory", external_territory_name, key_field)
    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Territory", {target_key_field: parent_remote_id}, "name")
        if name:
            return name

    if title_field_source:
        parent_title = frappe.db.get_value("External Territory", external_territory_name, title_field_source)
        if parent_title and frappe.db.exists("Territory", parent_title):
            return parent_title

    raw_stack = getattr(frappe.flags, "external_territory_sync_stack", None)
    if raw_stack is None:
        stack = []
    elif isinstance(raw_stack, (set, list, tuple)):
        stack = list(raw_stack)
    else:
        stack = [raw_stack]

    if external_territory_name in stack:
        return None

    stack.append(external_territory_name)
    frappe.flags.external_territory_sync_stack = stack
    try:
        sync_doc_by_key(
            source_doctype="External Territory",
            source_name=external_territory_name,
            target_doctype="Territory",
            key_field=key_field,
            title_field_source=title_field_source,
        )
    finally:
        if external_territory_name in stack:
            stack.remove(external_territory_name)

    if target_key_field and parent_remote_id:
        name = frappe.db.get_value("Territory", {target_key_field: parent_remote_id}, "name")
        if name:
            return name

    if title_field_source:
        parent_title = frappe.db.get_value("External Territory", external_territory_name, title_field_source)
        if parent_title and frappe.db.exists("Territory", parent_title):
            return parent_title

    return None


def _is_root_territory_doc(tgt, src, title_field_source: str | None) -> bool:
    candidates = {
        tgt.name,
        src.name,
        src.get(title_field_source) if title_field_source else None,
    }
    return "All Territories" in {v for v in candidates if v}


def _sync_child_tables(src, tgt, target_meta, company_abbr: str | None = None):
    table_fields = [df for df in target_meta.fields if df.fieldtype == "Table"]

    for df in table_fields:
        child_field = df.fieldname
        source_rows = src.get(child_field)
        if not source_rows:
            continue

        tgt.set(child_field, [])

        child_meta = frappe.get_meta(df.options)
        child_fields = {f.fieldname for f in child_meta.fields}
        child_field_map = {f.fieldname: f for f in child_meta.fields}

        for row in source_rows:
            if hasattr(row, "as_dict"):
                row = row.as_dict()

            child = tgt.append(child_field, {})
            for key, value in row.items():
                if key == "name" or key in SYSTEM_FIELDS:
                    continue
                if key not in child_fields:
                    continue
                # Resolve Link fields so values are valid on the target site.
                # This mirrors the main field loop in sync_doc_by_key and prevents
                # cross-company account validation errors (e.g. Supplier.accounts).
                cdf = child_field_map.get(key)
                if cdf and cdf.fieldtype == "Link" and value:
                    if cdf.options == "Account" and tgt.get("company"):
                        resolved = resolve_company_account(
                            value,
                            tgt.company,
                            company_abbr,
                        )
                    else:
                        resolved = resolve_link_value(
                            cdf.options,
                            value,
                            company_abbr=company_abbr,
                        )
                    if resolved:
                        value = resolved
                child.set(key, value)
# def _sync_child_tables(src, tgt, target_meta):
#     ROW_LINK_FIELDS = {
#     "po_detail",
#     "so_detail",
#     "purchase_order_item",
#     "sales_order_item",
#     "reference_detail_no",
#     "prevdoc_detail_docname",
#     "cost_center"
#     "project"
#     }
#     table_fields = [
#         df for df in target_meta.fields
#         if df.fieldtype == "Table"
#     ]

#     for df in table_fields:

#         child_field = df.fieldname

#         source_rows = src.get(child_field)

#         if not source_rows:
#             continue

#         # clear existing
#         tgt.set(child_field, [])

#         child_meta = frappe.get_meta(df.options)

#         child_fields = {
#             f.fieldname
#             for f in child_meta.fields
#         }

#         for row in source_rows:

#             # ✅ Convert Frappe Doc → dict safely
#             if hasattr(row, "as_dict"):
#                 row = row.as_dict()

#             child = tgt.append(child_field, {})

#             for key, value in row.items():

#                 if key == "name":
#                     continue

#                 if key in SYSTEM_FIELDS:
#                     continue

#                 if key in ROW_LINK_FIELDS:
#                     continue

#                 if key in child_fields:
#                     child.set(key, value)

def _apply_purchase_invoice_sync_rules(src, tgt, key_field):

    tgt.flags.ignore_pricing_rule = True


def _apply_payment_entry_sync_rules(src, tgt, key_field):
	pass


def _sync_parent_external_account(external_name: str, key_field: str, company: str) -> None:
    """
    Recursively sync a parent External Account into ERPNext Account
    before the child is saved. Uses a flag to prevent infinite loops.
    """
    stack = getattr(frappe.flags, "external_account_sync_stack", None)
    if stack is None:
        stack = []
        frappe.flags.external_account_sync_stack = stack

    if external_name in stack:
        return  # prevent infinite recursion

    stack.append(external_name)
    try:
        src = frappe.get_doc("External Account", external_name)

        tgt_key_field = _get_actual_target_key("Account", key_field)
        key_value = src.get(key_field)

        # Check if already synced
        if key_value and tgt_key_field:
            existing = frappe.db.get_value(
                "Account",
                {tgt_key_field: key_value, "company": company},
                "name",
            )
            if existing:
                return

        # Build and save the parent Account
        tgt = frappe.new_doc("Account")
        tgt.company = company

        target_meta = frappe.get_meta("Account")
        target_fields = {df.fieldname for df in target_meta.fields}

        for field, value in src.as_dict().items():
            if field in SYSTEM_FIELDS:
                continue
            if field == "parent_account":
                continue
            if field in target_fields:
                df = target_meta.get_field(field)
                if df and df.fieldtype == "Link" and value:
                    value = resolve_link_value(df.options, value)
                tgt.set(field, value)

        if tgt_key_field and key_value:
            tgt.set(tgt_key_field, key_value)

        tgt.company = company

        _apply_account_sync_rules(src=src, tgt=tgt, key_field=key_field)

        tgt.flags.ignore_root_company_validation = True
        tgt.save(ignore_permissions=True)

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Auto-sync of parent External Account failed: {external_name}",
        )
    finally:
        if external_name in stack:
            stack.remove(external_name)
            
def get_or_create_company_root(company, root_type):

    root = frappe.get_value(
        "Account",
        {
            "company": company,
            "root_type": root_type,
            "parent_account": ["is", "not set"]
        },
        "name"
    )

    if root:
        return root

    # Create if missing
    root_doc = frappe.get_doc({
        "doctype": "Account",
        "account_name": root_type,
        "company": company,
        "root_type": root_type,
        "is_group": 1
    })

    root_doc.insert(ignore_permissions=True)

    return root_doc.name
import frappe

def sync_external_account(external_account_name, company):

    external = frappe.get_doc("External Account", external_account_name)

    # If already synced → return existing
    existing = frappe.get_value(
        "Account",
        {"external_account": external.name, "company": company},
        "name"
    )
    if existing:
        return existing

    # 🔁 Step 1: Ensure Parent First
    if external.parent_account:
        parent_account = sync_external_account(external.parent_account, company)
    else:
        parent_account = get_or_create_company_root(company, external.root_type)

    # Step 2: Create Current Account
    account = frappe.get_doc({
        "doctype": "Account",
        "account_name": external.account_name,
        "company": company,
        "parent_account": parent_account,
        "is_group": external.is_group,
        "root_type": external.root_type,
        "external_account": external.name
    })

    account.insert(ignore_permissions=True)

    return account.name
    external = frappe.get_doc("External Account", external_account_name)

    # If already exists in Account, return
    existing = frappe.get_value(
        "Account",
        {"external_account": external.name, "company": company},
        "name"
    )
    if existing:
        return existing

    parent_account = None

    # 🔁 If external has parent → recursively ensure parent exists first
    if external.parent_account:
        parent_account = sync_external_account(external.parent_account, company)

    else:
        # This is top-most account
        # Attach to ERP root using root_type
        parent_account = get_or_create_company_root(company, external.root_type)

    # Now create current account
    account = frappe.get_doc({
        "doctype": "Account",
        "account_name": external.account_name,
        "company": company,
        "parent_account": parent_account,
        "is_group": external.is_group,
        "root_type": external.root_type,
        "external_account": external.name
    })

    account.insert(ignore_permissions=True)
    return account.name
