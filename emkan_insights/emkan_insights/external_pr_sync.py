import frappe
import json
from time import sleep
from frappe.utils import flt
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.override.accounts_controller import (
    custom_validate_company_linked_addresses,
)
from emkan_insights.emkan_insights.sync_guard import (
    acquire_sync_lock,
    copied_link_validation_disabled,
    party_validation_disabled,
    release_sync_lock,
    set_source_site_fields,
    temporarily_enable_transaction_items,
)

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

# Keep synchronous chunks small to limit accounting and stock lock duration.
INLINE_CHUNK_SIZE = 25
LOCK_RETRY_ATTEMPTS = 2
LOCK_RETRY_DELAY_SECONDS = 2

# ⭐ FIX: Added purchase_invoice_item and purchase_receipt return-link fields to ignore list
IGNORE_ITEM_FIELDS = {
    "purchase_order_item",
    "purchase_invoice_item",
    "purchase_receipt_item",
    "purchase_receipt_detail",
    "prevdoc_doctype",
    "prevdoc_docname"
}

# ⭐ NEW: Chunk size for splitting large bulk syncs into multiple background jobs.
# 300 keeps each job comfortably under the 2000s timeout even when records need
# submit() calls, negative-stock retries, or several link-resolution lookups.
CHUNK_SIZE = 300

# TTL for the per-doctype "target sync" lock that serialises sequential chunks.
# Mirrors external_stock_entry_sync / external_material_request_sync.
TARGET_SYNC_LOCK_TTL = 7200

ASSET_CATEGORY_ACCOUNT_FIELDS = (
    "fixed_asset_account",
    "accumulated_depreciation_account",
    "depreciation_expense_account",
    "capital_work_in_progress_account",
)


def _target_sync_lock_key(source_doctype):
    return f"external_target_sync_lock:{source_doctype}"


def _acquire_target_sync_lock(source_doctype):
    return bool(
        frappe.cache().set(
            _target_sync_lock_key(source_doctype),
            "1",
            nx=True,
            ex=TARGET_SYNC_LOCK_TTL,
        )
    )


def _release_target_sync_lock(source_doctype):
    frappe.cache().delete(_target_sync_lock_key(source_doctype))


def _enqueue_purchase_receipt_batch(source_doctype, names, batch_index=0):
    if batch_index * CHUNK_SIZE >= len(names):
        return

    frappe.enqueue(
        "emkan_insights.emkan_insights.external_pr_sync.sync_bulk_purchase_receipts",
        queue="long",
        job_name=f"pr_sync_chunk_{frappe.generate_hash(length=8)}_{batch_index}",
        timeout=2000,
        source_doctype=source_doctype,
        names=names,
        batch_index=batch_index,
        enqueue_after_commit=True,
    )


def _order_receipts_for_sync(names):
    """Place normal receipts before returns while preserving source order."""
    if not names:
        return []

    return_names = {
        row.name
        for row in frappe.get_all(
            "External Purchase Receipt",
            filters={"name": ["in", names]},
            fields=["name", "is_return", "return_against"],
        )
        if frappe.utils.cint(row.is_return) or row.return_against
    }

    normal_receipts = [name for name in names if name not in return_names]
    return_receipts = [name for name in names if name in return_names]
    return normal_receipts + return_receipts


def _get_table_fieldnames(meta):
    return {df.fieldname for df in meta.get_table_fields()}


def _clear_invalid_company_address(pr):
    custom_validate_company_linked_addresses(pr)


def _apply_sync_flags(doc):
    """Apply all necessary flags for sync operations"""
    doc.flags.ignore_permissions = True
    doc.flags.ignore_naming_series = True
    # Allow zero-valuation-rate items on every PR submit/cancel. Without this,
    # an item with no prior stock and no master valuation_rate (e.g. 03030160)
    # fails submit with "Valuation Rate ... is required to do accounting entries"
    # from get_valuation_rate. Mirrors external_delivery_note_sync.
    if getattr(doc, "doctype", None) == "Purchase Receipt":
        for item in doc.get("items", []):
            item.allow_zero_valuation_rate = 1
        _original_update_stock_ledger = doc.update_stock_ledger
        def _sync_update_stock_ledger(allow_negative_stock=False, via_landed_cost_voucher=False):
            return _original_update_stock_ledger(
                allow_negative_stock=True,
                via_landed_cost_voucher=via_landed_cost_voucher,
            )
        doc.update_stock_ledger = _sync_update_stock_ledger.__get__(doc, type(doc))


def _get_company_stock_account(company):
    account = frappe.db.get_value(
        "Company",
        company,
        "default_inventory_account",
    )
    if _account_is_local_stock_account(account, company):
        return account

    return frappe.db.get_value(
        "Account",
        {"account_type": "Stock", "is_group": 0, "company": company},
        "name",
    )


def _account_is_local_stock_account(account, company):
    if not account:
        return False

    values = frappe.db.get_value(
        "Account",
        account,
        ["company", "account_type", "is_group"],
        as_dict=True,
    )
    return bool(
        values
        and values.company == company
        and values.account_type == "Stock"
        and not values.is_group
    )


def _ensure_local_warehouse_stock_accounts(pr):
    """
    ERPNext's warehouse account map trusts Warehouse.account when it is filled.
    If that copied account does not exist locally, PR submit crashes while
    reading account currency. Replace invalid warehouse accounts with a local
    Stock account for this PR's company before GL generation.
    """
    stock_account = _get_company_stock_account(pr.company)
    if not stock_account:
        return

    warehouses = {
        item.get(fieldname)
        for item in pr.get("items", [])
        for fieldname in ("warehouse", "rejected_warehouse", "from_warehouse")
        if item.get(fieldname)
    }
    if pr.get("supplier_warehouse"):
        warehouses.add(pr.supplier_warehouse)

    repaired = []
    for warehouse in warehouses:
        current_account = frappe.db.get_value("Warehouse", warehouse, "account")
        if current_account and not _account_is_local_stock_account(current_account, pr.company):
            frappe.db.set_value("Warehouse", warehouse, "account", stock_account, update_modified=False)
            repaired.append(f"{warehouse}: {current_account} -> {stock_account}")

    if repaired:
        frappe.flags.warehouse_account_map = {}
        frappe.log_error(
            title=f"PR Sync: repaired warehouse stock accounts for {pr.name}",
            message="\n".join(repaired),
        )


def _ensure_local_asset_category_accounts(pr):
    asset_categories = {
        item.asset_category
        for item in pr.get("items", [])
        if item.get("is_fixed_asset") and item.get("asset_category")
    }
    if not asset_categories:
        return

    company_abbr = frappe.db.get_value("Company", pr.company, "abbr")
    rows = frappe.get_all(
        "Asset Category Account",
        filters={
            "parent": ["in", sorted(asset_categories)],
            "company_name": pr.company,
        },
        fields=["name", "parent", *ASSET_CATEGORY_ACCOUNT_FIELDS],
    )

    repaired = []
    for row in rows:
        updates = {}
        for fieldname in ASSET_CATEGORY_ACCOUNT_FIELDS:
            remote_account = row.get(fieldname)
            if not remote_account:
                continue

            local_account = resolve_company_account(
                remote_account,
                pr.company,
                company_abbr,
            )
            if local_account and local_account != remote_account:
                updates[fieldname] = local_account

        if not updates:
            continue

        frappe.db.set_value(
            "Asset Category Account",
            row.name,
            updates,
            update_modified=False,
        )
        repaired.append(f"{row.parent} ({row.name}): {updates}")

    if repaired:
        frappe.log_error(
            title=f"PR Sync: repaired asset category accounts for {pr.name}",
            message="\n".join(repaired),
        )


def _submit_pr_with_overflow_handling(pr_name, log_prefix="Purchase Receipt"):
    """
    Submit a Purchase Receipt via the ORM, clearing Material Request links if
    the submit fails on OverAllowanceError (the PR receipts more qty than the
    MR/allowance permits). Mirrors the Purchase Order sync's handling.

    ERPNext writes docstatus=1 before running on_submit(), while SLE creation
    happens later in on_submit(). Roll back the failed submit before clearing
    MR links so an incomplete submission can never be committed.

    Returns the submitted doc. Raises on any non-overflow error so the caller's
    savepoint rolls back and the record stays retryable.
    """
    from erpnext.controllers.status_updater import OverAllowanceError

    doc = frappe.get_doc("Purchase Receipt", pr_name)
    _apply_sync_flags(doc)
    _ensure_local_warehouse_stock_accounts(doc)
    _ensure_local_asset_category_accounts(doc)

    submit_savepoint = f"pr_submit_{frappe.generate_hash(length=10)}"
    frappe.db.savepoint(submit_savepoint)

    try:
        try:
            with temporarily_enable_transaction_items(doc):
                doc.submit()
            return doc
        except OverAllowanceError:
            # submit() writes docstatus before on_submit(). Roll back before
            # changing MR links so a failed on_submit cannot be committed as a
            # submitted Purchase Receipt without its SLE and GL entries.
            frappe.db.rollback(save_point=submit_savepoint)
            frappe.log_error(
                title=f"{log_prefix} {pr_name}: MR allowance exceeded",
                message=(
                    "Submit failed because the linked Material Request quantity "
                    "was exceeded. Clearing the MR links and retrying submit."
                ),
            )

            doc = frappe.get_doc("Purchase Receipt", pr_name)
            for item in doc.items:
                if item.material_request or item.material_request_item:
                    item.material_request = None
                    item.material_request_item = None

            _apply_sync_flags(doc)
            doc.save(ignore_permissions=True)

            with temporarily_enable_transaction_items(doc):
                doc.submit()
            return doc
    except Exception:
        frappe.db.rollback(save_point=submit_savepoint)
        raise


def _get_source_docstatus(ext_doc):
    """Safely get docstatus from source document."""
    docstatus = ext_doc.get("docstatus", 0)

    if isinstance(docstatus, str):
        try:
            docstatus = int(docstatus)
        except (ValueError, TypeError):
            docstatus = 0

    if not isinstance(docstatus, int) or docstatus not in (0, 1, 2):
        status = ext_doc.get("status", "")
        if isinstance(status, str):
            status_lower = status.lower().strip()
            if status_lower == "submitted":
                docstatus = 1
            elif status_lower == "cancelled":
                docstatus = 2
            else:
                docstatus = 0
        else:
            docstatus = 0

    return docstatus


def _copy_child_row_fields(row, target_child_doctype, extra_ignore=None):
    target_meta = frappe.get_meta(target_child_doctype)
    target_table_fields = _get_table_fieldnames(target_meta)
    ignored_fields = set(extra_ignore or [])
    row_data = {"doctype": target_child_doctype}

    for field, value in row.as_dict().items():
        if field in SYSTEM_FIELDS:
            continue
        if field in ignored_fields:
            continue
        if field in target_table_fields:
            continue
        if target_meta.has_field(field):
            row_data[field] = value

    return row_data


def _append_matching_child_table(target_doc, source_doc, table_fieldname):
    target_df = target_doc.meta.get_field(table_fieldname)
    if not target_df or not target_df.options:
        return

    source_rows = source_doc.get(table_fieldname) or []
    if not source_rows:
        return

    target_doc.set(table_fieldname, [])
    for row in source_rows:
        child_row = _copy_child_row_fields(row, target_df.options)
        if child_row.get("account_head"):
            company_abbr = frappe.db.get_value("Company", target_doc.company, "abbr") or ""
            child_row["account_head"] = resolve_company_account(
                child_row["account_head"], target_doc.company, company_abbr
            )
        target_doc.append(
            table_fieldname,
            child_row
        )


def clean_duplicate_pr_items(parent_name):
    frappe.db.sql("""
        DELETE t1 FROM `tabPurchase Receipt Item` t1
        INNER JOIN `tabPurchase Receipt Item` t2
        WHERE
            t1.name > t2.name
            AND t1.parent = t2.parent
            AND t1.parenttype = t2.parenttype
            AND t1.parenttype = 'Purchase Receipt'
            AND t1.parent = %s
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
    """, (parent_name,))


# =============================================================================
# RESOLUTION HELPERS
# =============================================================================
def _is_cancelled(doctype, name):
    """Return True if the given document exists and is cancelled (docstatus == 2)."""
    if not name:
        return False
    return frappe.db.get_value(doctype, name, "docstatus") == 2


def resolve_local_po(remote_or_local_po, company_abbr=None):
    if not remote_or_local_po:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Purchase Order
        return None if _is_cancelled("Purchase Order", name) else name

    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return _valid(prefixed)
    local_name = frappe.db.get_value("Purchase Order", {"remote_id": remote_or_local_po}, "name")
    if local_name:
        return _valid(local_name)
    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return _valid(remote_or_local_po)
    return None


def get_po_detail(purchase_order, item_code, remote_po_item=None):
    if not purchase_order:
        return None
    if remote_po_item:
        if frappe.db.exists("Purchase Order Item", {"name": remote_po_item, "parent": purchase_order}):
            return remote_po_item
        poi_meta = frappe.get_meta("Purchase Order Item")
        if poi_meta.has_field("custom_remote_id"):
            local_po_item = frappe.db.get_value("Purchase Order Item", {"parent": purchase_order, "custom_remote_id": remote_po_item}, "name")
            if local_po_item:
                return local_po_item
    return frappe.db.get_value("Purchase Order Item", {"parent": purchase_order, "item_code": item_code}, "name")


def sync_item_with_po_reference(item_row):
    """
    Keep PR item fields aligned with the linked Purchase Order Item.
    ERPNext validates these fields against the previous document during insert.
    """
    po_item = item_row.get("purchase_order_item")
    if not po_item:
        return

    po_values = frappe.db.get_value(
        "Purchase Order Item",
        po_item,
        ["project", "uom", "item_code"],
        as_dict=True,
    )
    if not po_values:
        return

    item_row["project"] = po_values.get("project")
    if po_values.get("uom"):
        item_row["uom"] = po_values.get("uom")
    if po_values.get("item_code"):
        item_row["item_code"] = po_values.get("item_code")


def _safeguard_po_links(pr):
    """
    Keep PR items valid against their linked Purchase Orders.

    ERPNext validates a Purchase Receipt against its referenced PO during
    insert:
    - validate_rate_with_reference_doc (Buying Settings maintain_same_rate)
      requires the PR item rate to equal the PO item rate.
    - validate_with_previous_doc requires the header supplier / company /
      currency to equal the PO header.

    Mirror the PO sync's SQ-link handling (external_po_sync.py:704-724):
    preserve the source PR values and DROP the PO reference instead of mutating
    them. Rate mismatches clear the offending item's PO link; PO header
    disagreements drop those items' links and rebase the PR header onto a
    surviving PO.
    """
    items = pr.get("items")
    if not items:
        return

    po_item_names = {
        item.purchase_order_item for item in items if item.get("purchase_order_item")
    }
    if po_item_names:
        po_item_rates = dict(
            frappe.get_all(
                "Purchase Order Item",
                filters={"name": ("in", po_item_names)},
                fields=["name", "rate"],
                as_list=True,
            )
        )
        for item in items:
            po_item = item.get("purchase_order_item")
            if not po_item:
                continue
            po_rate = po_item_rates.get(po_item)
            if po_rate is None:
                continue
            if abs(flt(item.rate - po_rate, item.precision("rate"))) >= 0.01:
                po_name = item.purchase_order
                item.purchase_order = None
                item.purchase_order_item = None
                company_abbr = frappe.db.get_value("Company",pr.company,"abbr")
                item.expense_account = resolve_company_account(
                item.expense_account, pr.company, company_abbr
            )
                frappe.log_error(
                    title=f"PR Sync: dropped PO link on {pr.name or 'new PR'}",
                    message=(
                        f"Row #{item.idx}: rate {flt(item.rate)} != PO item rate "
                        f"{flt(po_rate)} ({po_name}) — maintain_same_rate validation "
                        f"would reject the sync. PO reference cleared so the "
                        f"Purchase Receipt preserves the source rate."
                    ),
                )

    _align_inherited_po_header(pr)


def _align_inherited_po_header(pr):
    """
    Rebase the PR header supplier/company/currency onto its linked Purchase
    Orders when one exists; drop PO references from items whose PO disagrees.
    (See _safeguard_po_links for the full context; split out for readability.)
    """
    linked_po_names = {
        item.purchase_order for item in pr.get("items") if item.get("purchase_order")
    }
    if not linked_po_names:
        return

    po_headers = frappe.db.sql(
        """
        SELECT name, supplier, company, currency
        FROM `tabPurchase Order`
        WHERE name IN %(names)s
        """,
        {"names": list(linked_po_names)},
        as_dict=True,
    )
    if not po_headers:
        return

    base = po_headers[0]

    mismatched = set()
    for h in po_headers:
        if h.supplier and h.supplier != base.supplier:
            mismatched.add(h.name)
        elif h.company and h.company != base.company:
            mismatched.add(h.name)
        elif (h.currency or base.currency) and h.currency != base.currency:
            mismatched.add(h.name)

    for item in pr.get("items"):
        if item.get("purchase_order") in mismatched:
            po_name = item.purchase_order
            item.purchase_order = None
            item.purchase_order_item = None
            frappe.log_error(
                title=f"PR Sync: dropped PO link on {pr.name or 'new PR'}",
                message=(
                    f"Linked PO '{po_name}' has supplier/company/currency that "
                    f"disagrees with '{base.name}'. PO reference cleared so the "
                    "Purchase Receipt stays internally consistent."
                ),
            )

    if base.supplier:
        pr.supplier = base.supplier
    if base.company:
        pr.company = base.company
    if base.currency:
        pr.currency = base.currency


def resolve_local_mr(remote_or_local_mr, company_abbr=None):
    if not remote_or_local_mr:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Material Request
        return None if _is_cancelled("Material Request", name) else name

    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return _valid(prefixed)
    local_name = frappe.db.get_value("Material Request", {"remote_id": remote_or_local_mr}, "name")
    if local_name:
        return _valid(local_name)
    if frappe.db.exists("Material Request", remote_or_local_mr):
        return _valid(remote_or_local_mr)
    return None


def get_mr_detail(material_request, item_code):
    if not material_request:
        return None
    return frappe.db.get_value("Material Request Item", {"parent": material_request, "item_code": item_code}, "name")


def resolve_local_pi(remote_or_local_pi, company_abbr=None):
    if not remote_or_local_pi:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Purchase Invoice
        return None if _is_cancelled("Purchase Invoice", name) else name

    if company_abbr and not remote_or_local_pi.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pi}"
        if frappe.db.exists("Purchase Invoice", prefixed):
            return _valid(prefixed)
    pi_meta = frappe.get_meta("Purchase Invoice")
    for candidate in ("custom_remote_id", "remote_id"):
        if pi_meta.has_field(candidate):
            local_name = frappe.db.get_value("Purchase Invoice", {candidate: remote_or_local_pi}, "name")
            if local_name:
                return _valid(local_name)
            break
    if frappe.db.exists("Purchase Invoice", remote_or_local_pi):
        return _valid(remote_or_local_pi)
    return None


def get_pi_detail(purchase_invoice, item_code, remote_pi_item=None):
    if not purchase_invoice:
        return None
    if remote_pi_item:
        if frappe.db.exists("Purchase Invoice Item", {"name": remote_pi_item, "parent": purchase_invoice}):
            return remote_pi_item
        pii_meta = frappe.get_meta("Purchase Invoice Item")
        if pii_meta.has_field("custom_remote_id"):
            local_pi_item = frappe.db.get_value("Purchase Invoice Item", {"parent": purchase_invoice, "custom_remote_id": remote_pi_item}, "name")
            if local_pi_item:
                return local_pi_item
    return frappe.db.get_value("Purchase Invoice Item", {"parent": purchase_invoice, "item_code": item_code}, "name")


def resolve_local_pr_for_item(remote_or_local_pr, company_abbr=None):
    if not remote_or_local_pr:
        return None

    def _valid(name):
        # ⭐ FIX: never link to a cancelled Purchase Receipt
        return None if _is_cancelled("Purchase Receipt", name) else name

    if company_abbr and not remote_or_local_pr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pr}"
        if frappe.db.exists("Purchase Receipt", prefixed):
            return _valid(prefixed)
    pr_meta = frappe.get_meta("Purchase Receipt")
    for candidate in ("custom_remote_id", "remote_id"):
        if pr_meta.has_field(candidate):
            local_name = frappe.db.get_value("Purchase Receipt", {candidate: remote_or_local_pr}, "name")
            if local_name:
                return _valid(local_name)
            break
    if frappe.db.exists("Purchase Receipt", remote_or_local_pr):
        return _valid(remote_or_local_pr)
    return None


def _resolve_external_purchase_receipt(remote_pr, company_abbr=None):
    """Return the External Purchase Receipt that represents ``remote_pr``."""
    if not remote_pr:
        return None

    candidates = [remote_pr]
    if company_abbr and not remote_pr.startswith(f"{company_abbr}-"):
        candidates.insert(0, f"{company_abbr}-{remote_pr}")

    for candidate in candidates:
        if frappe.db.exists("External Purchase Receipt", candidate):
            return candidate

    meta = frappe.get_meta("External Purchase Receipt")
    if meta.has_field("remote_id"):
        return frappe.db.get_value(
            "External Purchase Receipt",
            {"remote_id": ["in", candidates]},
            "name",
        )

    return None


def _sync_return_parent(ext_pr, company_abbr, strict, dependency_stack):
    """Ensure a return's original Purchase Receipt is locally submitted.

    A cancelled source parent must remain Submitted locally until its cancelled
    return has also been synced. Otherwise ERPNext rejects the return_against
    link. The caller receives a flag telling it to cancel that parent after the
    return reaches Cancelled through the ORM.
    """
    remote_parent = ext_pr.get("return_against")
    external_parent = _resolve_external_purchase_receipt(remote_parent, company_abbr)
    effective_parent = _resolve_effective_external_return_parent(
        ext_pr, external_parent
    )
    parent_to_resolve = effective_parent or remote_parent

    local_parent = resolve_local_pr_for_item(parent_to_resolve, company_abbr)
    if local_parent:
        return local_parent, _return_parent_should_be_cancelled_after(
            ext_pr, effective_parent
        )

    if not effective_parent:
        return None, False

    if effective_parent == ext_pr.name or effective_parent in dependency_stack:
        frappe.log_error(
            title=f"PR Sync: cyclic return dependency for {ext_pr.name}",
            message=(
                f"Cannot sync return '{ext_pr.name}' because its return dependency "
                f"resolves to '{effective_parent}', which is already being processed."
            ),
        )
        return None, False

    _sync_bulk_purchase_receipts(
        [effective_parent],
        strict=strict,
        dependency_stack=(*dependency_stack, ext_pr.name),
        _defer_cancellation=True,
    )
    return (
        resolve_local_pr_for_item(parent_to_resolve, company_abbr),
        _return_parent_should_be_cancelled_after(ext_pr, effective_parent),
    )


def _resolve_effective_external_return_parent(ext_return, external_parent):
    """Use the active amendment whose party matches the return, when present."""
    if not external_parent:
        return None

    current_parent = external_parent
    visited = set()
    while current_parent and current_parent not in visited:
        visited.add(current_parent)
        amendments = frappe.get_all(
            "External Purchase Receipt",
            filters={"amended_from": current_parent},
            fields=["name", "docstatus", "supplier", "company", "currency"],
            order_by="creation desc",
        )
        if not amendments:
            break

        for amendment in amendments:
            if (
                frappe.utils.cint(amendment.docstatus) != 2
                and amendment.supplier == ext_return.get("supplier")
                and amendment.company == ext_return.get("company")
                and (
                    not amendment.currency
                    or not ext_return.get("currency")
                    or amendment.currency == ext_return.get("currency")
                )
            ):
                return amendment.name

        current_parent = amendments[0].name

    return external_parent


def _return_parent_should_be_cancelled_after(ext_return, external_parent):
    """Return True when both source documents are already Cancelled."""
    if _get_source_docstatus(ext_return) != 2 or not external_parent:
        return False

    parent_status = frappe.db.get_value(
        "External Purchase Receipt", external_parent, "docstatus"
    )
    return frappe.utils.cint(parent_status) == 2


def _has_pending_cancelled_return(remote_parent):
    """Keep a cancelled parent submitted until its cancelled return is synced."""
    return bool(
        frappe.db.exists(
            "External Purchase Receipt",
            {
                "return_against": remote_parent,
                "docstatus": 2,
                "is_synced": 0,
            },
        )
    )


def _cancel_submitted_return_parent(parent_name, return_name):
    """Cancel a deferred parent through the ORM after its return is cancelled."""
    if frappe.utils.cint(
        frappe.db.get_value("Purchase Receipt", parent_name, "docstatus")
    ) != 1:
        return

    parent = frappe.get_doc("Purchase Receipt", parent_name)
    _apply_sync_flags(parent)
    with temporarily_enable_transaction_items(parent):
        _safe_cancel_doc(
            parent,
            log_prefix=(
                f"Purchase Receipt {parent_name} after cancelled return {return_name}"
            ),
        )


def resolve_amendment_parent(remote_amended_from, company_abbr=None):
    """
    Resolve the LOCAL Purchase Receipt to link via amended_from.

    An amendment is only legal when the parent is Cancelled — ERPNext's
    validate_amended_from throws "cannot be amended because it is not
    cancelled" otherwise. So this resolver returns a name only when the
    referenced local PR exists AND is cancelled (docstatus=2). The name may
    differ from the source (company_abbr prefix), so this mirrors
    resolve_local_pr_for_item but accepts only the cancelled parent.
    """
    if not remote_amended_from:
        return None

    candidates = [remote_amended_from]
    if company_abbr and not remote_amended_from.startswith(f"{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_amended_from}")

    pr_meta = frappe.get_meta("Purchase Receipt")
    for candidate in ("custom_remote_id", "remote_id"):
        if pr_meta.has_field(candidate):
            candidates.append(frappe.db.get_value(
                "Purchase Receipt", {candidate: remote_amended_from}, "name"
            ))

    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        if frappe.db.exists("Purchase Receipt", name):
            docstatus = frappe.db.get_value("Purchase Receipt", name, "docstatus")
            if frappe.utils.cint(docstatus) == 2:
                return name
            return None  # parent exists but is not cancelled — amendment not allowed

    return None


def _sync_amendment_parent(ext_pr, company_abbr, strict, dependency_stack):
    """Ensure an amendment's source document exists locally as Cancelled."""
    remote_parent = ext_pr.get("amended_from")
    local_parent = resolve_amendment_parent(remote_parent, company_abbr)
    if local_parent:
        return local_parent

    external_parent = _resolve_external_purchase_receipt(remote_parent, company_abbr)
    if not external_parent:
        return None

    if external_parent == ext_pr.name or external_parent in dependency_stack:
        frappe.log_error(
            title=f"PR Sync: cyclic amendment dependency for {ext_pr.name}",
            message=(
                f"Cannot sync amendment '{ext_pr.name}' because amended_from "
                f"resolves to '{external_parent}', which is already being processed."
            ),
        )
        return None

    _sync_bulk_purchase_receipts(
        [external_parent],
        strict=strict,
        dependency_stack=(*dependency_stack, ext_pr.name),
        _force_cancellation=True,
    )
    return resolve_amendment_parent(remote_parent, company_abbr)


def get_pr_detail_for_item(purchase_receipt, item_code, remote_pr_item=None):
    """
    Get the LOCAL Purchase Receipt Item name for a return reference.
    
    IMPORTANT: This function NEVER returns the remote_pr_item value directly.
    It always looks up and returns the actual local database name.
    """
    if not purchase_receipt:
        return None
    
    local_item_name = None
    
    # Strategy 1: Find by custom_remote_id (most accurate for synced data)
    if remote_pr_item:
        pri_meta = frappe.get_meta("Purchase Receipt Item")
        if pri_meta.has_field("custom_remote_id"):
            local_item_name = frappe.db.get_value(
                "Purchase Receipt Item",
                {"parent": purchase_receipt, "custom_remote_id": remote_pr_item},
                "name"
            )
    
    # Strategy 2: Fallback to finding by item_code
    if not local_item_name and item_code:
        # Try exact match first
        local_item_name = frappe.db.get_value(
            "Purchase Receipt Item",
            {"parent": purchase_receipt, "item_code": item_code},
            "name"
        )
        
        # If no exact match, try case-insensitive match
        if not local_item_name:
            local_item_name = frappe.db.sql("""
                SELECT name FROM `tabPurchase Receipt Item`
                WHERE parent = %s AND LOWER(item_code) = LOWER(%s)
                LIMIT 1
            """, (purchase_receipt, item_code))
            if local_item_name:
                local_item_name = local_item_name[0][0]
    
    # SECURITY CHECK: Never return the remote_pr_item value
    # If somehow we got the remote ID, treat it as invalid
    if local_item_name and local_item_name == remote_pr_item:
        # Verify this is actually a valid local item (not just the remote ID leaking through)
        is_valid = frappe.db.exists(
            "Purchase Receipt Item", 
            {"name": local_item_name, "parent": purchase_receipt}
        )
        if not is_valid:
            local_item_name = None
    
    return local_item_name


def _set_docstatus_directly(doctype, docname, docstatus):

    raise frappe.ValidationError(
        f"_set_docstatus_directly('{doctype}', '{docname}', {docstatus}) is "
        f"not allowed: raw SQL docstatus changes bypass Stock Ledger Entry "
        f"and GL Entry creation/reversal. Use the ORM submit() / cancel() path."
    )


def _safe_cancel_doc(doc, log_prefix=""):

    try:
        doc.flags.ignore_permissions = True
        for item in doc.get("items", []):
            item.allow_zero_valuation_rate = 1
        doc.cancel()  # ORM cancel — reverses SLE + GL Entries
        return True
    except Exception as cancel_err:
        frappe.log_error(
            title=f"CRITICAL: {log_prefix} ORM cancel failed — manual intervention required",
            message=(
                f"Error: {str(cancel_err)}\n\n{frappe.get_traceback()}\n\n"
                f"The document has NOT been force-cancelled. Its Stock Ledger Entries "
                f"and GL Entries remain active. Investigate why cancel() failed and "
                f"cancel the document manually via ERPNext to ensure ledger integrity."
            )
        )
        raise


# =============================================================================
# ENTRY POINT (WHITELIST)
# =============================================================================
@frappe.whitelist()
@filter_kwargs
def sync_external_purchase_receipt_docs(source_doctype, names, sync_now=False):
    if isinstance(names, str):
        names = json.loads(names)

    # ⭐ NEW: De-duplicate incoming names while preserving order. This guarantees
    # the same record can never end up in two different chunks, which is the
    # only scenario that could cause a duplicate Purchase Receipt to be created
    # when chunks run in parallel on different workers.
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure (including failed
        # submit/cancel and negative-stock errors) aborts everything.
        #
        # Wrap in copied_link_validation_disabled() just like the background
        # path (sync_bulk_purchase_receipts): copied header links reference
        # remote records that may not exist here or belong to a different local
        # party, so validate_party_address_and_contact would otherwise abort
        # PR insert with "Contact Person does not belong to ...".
        target_source = source_doctype or "External Purchase Receipt"
        if not _acquire_target_sync_lock(target_source):
            frappe.throw(f"{target_source} target sync is already running")

        try:
            ordered_names = _order_receipts_for_sync(unique_names)
            return run_chunks_fail_fast(
                ordered_names,
                INLINE_CHUNK_SIZE,
                lambda chunk: _run_receipt_sync_chunk(chunk),
            )
        finally:
            _release_target_sync_lock(target_source)

    # Background path: split into chunks of CHUNK_SIZE, but chain each batch so
    # only ONE batch runs at a time. Parallel chunks on multiple workers gave no
    # ordering guarantee, so a return could process before its return_against
    # parent (and an amendment before its cancelled parent) causing
    # "Purchase Receipt None not found". Each batch enqueues the next one on
    # completion (see sync_bulk_purchase_receipts). The per-doctype target lock
    # prevents two such chains from overlapping.
    target_source = source_doctype or "External Purchase Receipt"
    if not _acquire_target_sync_lock(target_source):
        return f"{target_source} target sync is already running"

    try:
        _enqueue_purchase_receipt_batch(target_source, unique_names, batch_index=0)
    except Exception:
        # If enqueue itself fails (Redis down, serialization error, etc.),
        # release the lock immediately so a subsequent run isn't blocked for
        # the full TTL. Without this the lock would only be released by the
        # background worker, which never starts in this failure mode.
        _release_target_sync_lock(target_source)
        raise

    total_batches = (len(unique_names) + CHUNK_SIZE - 1) // CHUNK_SIZE
    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{total_batches} sequential batch(es) of up to {CHUNK_SIZE} each"
    )

# =============================================================================
# BACKGROUND WORKER
# =============================================================================
def _validate_or_clear_return_link(pr, ext_pr):
    """
    Validate that all return item references are valid before insert.
    Clears return links if any references are invalid.
    """
    if not pr.get("is_return") or not pr.get("return_against"):
        return

    # Get all valid item names AND item_codes from the target PR
    target_items = frappe.get_all(
        "Purchase Receipt Item",
        filters={"parent": pr.return_against},
        fields=["name", "item_code"],
    )
    
    if not target_items:
        # Target PR has no items - can't be a valid return
        frappe.log_error(
            title=f"PR Sync: return_against PR has no items: {pr.return_against}",
            message=f"Cannot create return PR because target {pr.return_against} has no items. Dropping return link."
        )
        pr.return_against = None
        if pr.meta.has_field("is_return"):
            pr.is_return = 0
        return

    # Build lookup structures
    valid_item_names = {r.name for r in target_items}
    item_code_to_row = {}
    for r in target_items:
        key = (r.item_code or "").strip().lower()
        # Keep first match for each item_code
        if key not in item_code_to_row:
            item_code_to_row[key] = r

    has_invalid_ref = False
    items_to_fix = []
    link_field = "purchase_receipt_item"

    for d in pr.items:
        item_needs_fix = False
        
        # Check 1: Validate purchase_receipt_item if set
        if d.get(link_field):
            pr_detail = d.get(link_field)
            
            # Check if it's a valid local item name
            if pr_detail not in valid_item_names:
                # This is likely a remote ID that leaked through - try to fix
                key = (d.item_code or "").strip().lower()
                ref_row = item_code_to_row.get(key)
                
                if ref_row:
                    items_to_fix.append((d, ref_row.name))
                else:
                    item_needs_fix = True
                    frappe.log_error(
                        title=f"PR Sync: invalid purchase_receipt_item '{pr_detail}'",
                        message=(
                            f"Item '{d.item_code}' references non-existent PR item '{pr_detail}' "
                            f"in {pr.return_against}. No matching item_code found either."
                        )
                    )
        
        # Check 2: Validate item_code exists in target
        if not item_needs_fix and d.get("item_code"):
            key = (d.item_code or "").strip().lower()
            canonical = item_code_to_row.get(key)
            
            if canonical is None:
                item_needs_fix = True
                frappe.log_error(
                    title=f"PR Sync: item_code not found in return_against",
                    message=f"Item '{d.item_code}' does not exist in {pr.return_against}"
                )

        if item_needs_fix:
            has_invalid_ref = True

    # Apply fixes for items that can be corrected
    for d, correct_pr_detail in items_to_fix:
        d.purchase_receipt_item = correct_pr_detail
        # Also ensure purchase_receipt is set correctly
        d.purchase_receipt = pr.return_against

    # If we have unfixable references, drop the return link entirely
    if has_invalid_ref:
        frappe.log_error(
            title=f"PR Sync: clearing return link for {pr.name or ext_pr.name}",
            message=(
                f"return_against={pr.return_against}\n"
                f"Item codes on this document: {[d.item_code for d in pr.items]}\n"
                f"Valid item codes in return_against: {sorted(item_code_to_row.keys())}\n\n"
                f"Dropping return_against/is_return so the receipt can still sync as a normal receipt. "
                f"Please review manually."
            ),
        )
        pr.return_against = None
        if pr.meta.has_field("is_return"):
            pr.is_return = 0
        # Also clear item-level return references
        for d in pr.items:
            d.purchase_receipt = None
            d.purchase_receipt_item = None


def _ensure_no_remote_ids_in_return_refs(pr):
    """
    Safety net: Ensure purchase_receipt_item points to the local child-row name.
    This should never happen if the above functions work correctly, but we check
    anyway to prevent validation errors.
    """
    if not pr.get("is_return"):
        return
    
    return_against = pr.get("return_against")
    if not return_against:
        return
    
    # Get all valid item names from the target PR
    valid_names = set(frappe.get_all(
        "Purchase Receipt Item",
        filters={"parent": return_against},
        pluck="name"
    ))
    
    for d in pr.items:
        remote_pr_item = d.get("purchase_receipt_item")
        if remote_pr_item and remote_pr_item in valid_names:
            d.purchase_receipt = return_against
            continue

        local_name = get_pr_detail_for_item(return_against, d.item_code, remote_pr_item)
        if local_name:
            d.purchase_receipt_item = local_name
            d.purchase_receipt = return_against
        else:
            # Can't fix - clear the return reference for this item
            d.purchase_receipt = None
            d.purchase_receipt_item = None
            
def sync_bulk_purchase_receipts(names, source_doctype="External Purchase Receipt", batch_index=0):
    # Background worker for one sequential batch. Processes the CHUNK_SIZE chunk
    # for this batch_index, then chains the next batch (or releases the target
    # lock when all batches are done) so only one batch runs at a time.
    # Copied header links (address / contact) reference remote records that
    # may not exist here or belong to a different local party. Skip those
    # validators for the whole batch so insert/submit/cancel don't abort.
    if isinstance(names, str):
        names = json.loads(names)

    batch_index = int(batch_index or 0)
    if batch_index == 0:
        names = _order_receipts_for_sync(names)
    chunk = names[batch_index * CHUNK_SIZE:(batch_index + 1) * CHUNK_SIZE]

    try:
        with copied_link_validation_disabled(), party_validation_disabled():
            results = _sync_bulk_purchase_receipts(chunk)
    except Exception:
        # Chain aborted: release the target lock so a failed run does not
        # block subsequent runs until TTL expiry.
        _release_target_sync_lock(source_doctype)
        raise

    next_batch_index = batch_index + 1
    if next_batch_index * CHUNK_SIZE < len(names):
        _enqueue_purchase_receipt_batch(source_doctype, names, next_batch_index)
    else:
        frappe.db.after_commit.add(
            lambda: _release_target_sync_lock(source_doctype)
        )

    return results


def _run_receipt_sync_chunk(names):
    # Strict (sync_now / sequential pipeline) variant. Must apply the same
    # copied_link_validation_disabled() guard as the background path so the
    # party/contact validators are skipped consistently in both entry points.
    lock_errors = (frappe.QueryDeadlockError, frappe.QueryTimeoutError)

    for attempt in range(LOCK_RETRY_ATTEMPTS):
        try:
            with copied_link_validation_disabled(), party_validation_disabled():
                return _sync_bulk_purchase_receipts(names, strict=True)
        except lock_errors:
            frappe.db.rollback()
            if attempt == LOCK_RETRY_ATTEMPTS - 1:
                raise
            sleep(LOCK_RETRY_DELAY_SECONDS)


def _sync_bulk_purchase_receipts(
    names,
    strict=False,
    _retry_pass=False,
    dependency_stack=(),
    _defer_cancellation=False,
    _force_cancellation=False,
):
    """
    Core per-chunk sync loop.

    Missing return parents are synced recursively before their return. The
    dependency stack prevents malformed cyclic references from recursing. A
    second pass retains the existing fallback for parents that are unavailable.
    """
    results = []
    pending_submit_retry = []
    pending_return_retry = []

    for name in names:
        savepoint = f"pr_sync_{frappe.generate_hash(length=10)}"
        _lock_name = None
        savepoint_created = False

        try:
            # ⭐ Per-record advisory lock: prevents two parallel workers from
            # processing the same External Purchase Receipt simultaneously,
            # which would otherwise cause duplicate PR creation or race conditions.
            # lock_timeout=0 means fail immediately if another worker holds the lock.
            _lock_name = acquire_sync_lock("External Purchase Receipt", name, lock_timeout=0)
            frappe.db.savepoint(savepoint)
            savepoint_created = True

            ext_pr = frappe.get_doc("External Purchase Receipt", name)
            sync_started_at = ext_pr.modified
            remote_id = ext_pr.name
            source_docstatus = _get_source_docstatus(ext_pr)
            defer_source_cancellation = source_docstatus == 2 and (
                not _force_cancellation
                and (
                    _defer_cancellation
                    or _has_pending_cancelled_return(remote_id)
                )
            )
            company_abbr = frappe.db.get_value('Company', ext_pr.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Purchase Receipt Sync: no company abbr found for company '{ext_pr.company}' on {remote_id}.",
                    "ExternalPurchaseReceipt Sync - missing company abbr"
                )

            target_name = remote_id

            remote_id_field = None
            pr_meta = frappe.get_meta("Purchase Receipt")
            for candidate in ("custom_remote_id", "remote_id"):
                if pr_meta.has_field(candidate):
                    remote_id_field = candidate
                    break

            if not remote_id_field:
                frappe.throw("Purchase Receipt has neither 'custom_remote_id' nor 'remote_id' field - cannot track sync identity.")

            existing_pr = frappe.db.get_value("Purchase Receipt", {remote_id_field: remote_id}, ["name", "docstatus"])

            if existing_pr:
                existing_name, existing_docstatus = existing_pr

                if source_docstatus != existing_docstatus:
                    try:
                        if source_docstatus == 2:
                            # ORM cancel — reverses SLE + GL Entries properly.
                            # Raw SQL cancel (_set_docstatus_directly) is intentionally disabled.
                            doc = frappe.get_doc("Purchase Receipt", existing_name)
                            _apply_sync_flags(doc)
                            if existing_docstatus == 0:
                                # Draft → must submit first (cancel requires docstatus=1)
                                _submit_pr_with_overflow_handling(existing_name)
                                doc = frappe.get_doc("Purchase Receipt", existing_name)
                            if not defer_source_cancellation:
                                with temporarily_enable_transaction_items(doc):
                                    _safe_cancel_doc(doc, log_prefix=f"Purchase Receipt {existing_name}")
                        elif source_docstatus == 1 and existing_docstatus == 0:
                            _submit_pr_with_overflow_handling(existing_name)
                        elif source_docstatus == 0 and existing_docstatus == 1:
                            frappe.log_error(
                                title=f"Cannot unsubmit Purchase Receipt: {existing_name}",
                                message=f"Source docstatus is 0 (Draft) but local is 1 (Submitted). Cannot unsubmit."
                            )
                    except Exception as status_error:
                        if strict:
                            raise
                        if "NegativeStockError" in frappe.get_traceback():
                            pending_submit_retry.append(existing_name)
                        frappe.log_error(
                            title=f"Purchase Receipt Status Update Error: {existing_name}",
                            message=f"Failed to update docstatus: {frappe.get_traceback()}"
                        )

                frappe.db.set_value("External Purchase Receipt", name, "is_synced", 1, update_modified=False)
                results.append({"name": existing_name, "status": "exists"})
                continue

            # -----------------------------------------------------
            # NEW PURCHASE RECEIPT
            # -----------------------------------------------------
            pr = frappe.new_doc("Purchase Receipt")
            pr.set(remote_id_field, remote_id)
            pr.name = target_name
            pr.flags.name_set = True
            _apply_sync_flags(pr)

            set_source_site_fields(pr, site_url=getattr(ext_pr, "source_site", None))

            pr_table_fields = _get_table_fieldnames(pr_meta)
            for field, value in ext_pr.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in pr_table_fields
                    and field not in ["remote_id", "custom_remote_id", "status", "return_against", "is_synced", "amended_from"]
                    and hasattr(pr, field)
                ):
                    pr.set(field, value)

            # ------------------------------------------------
            # RESOLVE AMENDED FROM (Header)
            # Legal only when the parent PR is Cancelled. Resolve the remote
            # value to the local PR and only set it when that parent is
            # docstatus=2; otherwise leave it blank so the insert does not fail
            # with "cannot be amended because it is not cancelled".
            # ------------------------------------------------
            remote_amended_from = ext_pr.get("amended_from")
            if remote_amended_from:
                pr.amended_from = _sync_amendment_parent(
                    ext_pr,
                    company_abbr,
                    strict,
                    dependency_stack,
                )
                if not pr.amended_from:
                    frappe.throw(
                        f"Cannot sync amendment '{name}' because cancelled Purchase "
                        f"Receipt '{remote_amended_from}' is unavailable."
                    )
            else:
                pr.amended_from = None

            # ------------------------------------------------
            # RESOLVE RETURN AGAINST (Header)
            # A return (is_return=1) is only legal when return_against points at
            # a real, resolvable local PR. If the parent cannot be resolved (e.g.
            # it lives in a later sequential batch and isn't synced yet) we must
            # NOT leave is_return=1 with return_against=None: ERPNext's status
            # updater then reads return_against (=None) as a parent and throws
            # "Purchase Receipt None not found" on submit. Mirror the Delivery
            # Note sync: keep is_return=1 only when return_against resolves.
            # ------------------------------------------------
            local_return_against = None
            cancel_return_parent_after_sync = False
            if ext_pr.get("return_against"):
                (
                    local_return_against,
                    cancel_return_parent_after_sync,
                ) = _sync_return_parent(
                    ext_pr,
                    company_abbr,
                    strict,
                    dependency_stack,
                )
                if not local_return_against:
                    if not _retry_pass:
                        # ── BEFORE (old behaviour) ────────────────────────────────────────
                        # The parent PR was silently not found → return_against/is_return
                        # were cleared → the return synced as a normal receipt (data
                        # corruption when the parent later arrived in a different batch).
                        #
                        # ── AFTER (new behaviour) ────────────────────────────────────────
                        # Parent hasn't been inserted yet (it arrives later in this chunk
                        # or in a different chunk). Roll back this record's savepoint and
                        # add it to pending_return_retry. After the main loop has processed
                        # all other entries the parent will (usually) exist, and the retry
                        # pass can resolve it correctly. The advisory lock is released by
                        # the finally block that still runs after `continue`.
                        frappe.db.rollback(save_point=savepoint)
                        pending_return_retry.append(name)
                        results.append({"name": name, "status": "deferred_return"})
                        continue
                    else:
                        # Retry pass: parent still not found (it lives in a different
                        # sync batch that hasn't run yet).
                        #
                        # We CANNOT fall back to syncing as a normal receipt: return
                        # entries carry negative quantities, so base_grand_total < 0
                        # and ERPNext's validate_value("base_grand_total", ">=", 0)
                        # will reject the insert. Skip this record and leave it
                        # un-synced (is_synced=0) so the next scheduled sync run
                        # retries it after the parent batch has completed.
                        if strict:
                            frappe.throw(
                                f"Cannot sync return '{name}' because original Purchase "
                                f"Receipt '{ext_pr.return_against}' is unavailable."
                            )
                        frappe.log_error(
                            title=f"PR Sync: return_against '{ext_pr.return_against}' still unresolved — record skipped",
                            message=(
                                f"return_against='{ext_pr.return_against}' could not be resolved "
                                f"even after the main chunk completed. The parent PR is likely in a "
                                f"different sync batch that hasn't run yet. "
                                f"Skipping '{name}' (is_synced left as 0) so it is retried "
                                f"automatically on the next sync run once the parent is available."
                            ),
                        )
                        frappe.db.rollback(save_point=savepoint)
                        results.append({"name": name, "status": "skipped_parent_not_synced"})
                        continue

            if local_return_against:
                pr.return_against = local_return_against
                pr.is_return = 1
            else:
                pr.return_against = None
                pr.is_return = 0

            # -----------------------------
            # ITEMS (Mirroring SI Logic Exactly)
            # -----------------------------
            pr.set("items", [])
            pr_item_meta = frappe.get_meta("Purchase Receipt Item")
            has_custom_remote_id = pr_item_meta.has_field("custom_remote_id")

            for row in ext_pr.items:
                # 1. Copy fields exactly like Sales Invoice does
                item_row = {}
                for field, value in row.as_dict().items():
                    if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                        item_row[field] = value

                # 2. Validate item exists locally
                item_code = item_row.get("item_code")
                if not item_code or not frappe.db.exists("Item", item_code):
                    frappe.log_error(
                        title=f"PR Sync: Item not found or empty - {item_code}",
                        message=f"Skipping item for External PR '{remote_id}'."
                    )
                    continue

                # ⭐ Resolve Account-type link fields to local company accounts
                for acct_field in ("expense_account", "provisional_expense_account"):
                    if item_row.get(acct_field):
                        item_row[acct_field] = resolve_company_account(
                            item_row[acct_field], ext_pr.company, company_abbr
                        )

                # ⚠️ BRUTE FORCE SAFETY NET 1: Remove any remote leak immediately
                item_row.pop("purchase_receipt_item", None)
                item_row.pop("purchase_receipt_detail", None)
                item_row.pop("purchase_receipt", None)

                # 3. Resolve PO
                if row.get("purchase_order"):
                    local_po = resolve_local_po(row.purchase_order, company_abbr)
                    item_row["purchase_order"] = local_po
                    item_row["purchase_order_item"] = get_po_detail(
                        local_po, row.item_code, row.get("purchase_order_item")
                    ) if local_po else None
                    sync_item_with_po_reference(item_row)
                else:
                    item_row["purchase_order"] = None
                    item_row["purchase_order_item"] = None

                # 4. Resolve MR
                if row.get("material_request"):
                    local_mr = resolve_local_mr(row.material_request, company_abbr)
                    item_row["material_request"] = local_mr
                    item_row["material_request_item"] = get_mr_detail(
                        local_mr, row.item_code
                    ) if local_mr else None
                else:
                    item_row["material_request"] = None
                    item_row["material_request_item"] = None

                # 5. Resolve PI
                if row.get("purchase_invoice"):
                    local_pi = resolve_local_pi(row.purchase_invoice, company_abbr)
                    item_row["purchase_invoice"] = local_pi
                    item_row["purchase_invoice_item"] = get_pi_detail(
                        local_pi, row.item_code, row.get("purchase_invoice_item")
                    ) if local_pi else None
                else:
                    item_row["purchase_invoice"] = None
                    item_row["purchase_invoice_item"] = None

                # 6. Resolve purchase_receipt_item using the original source row name
                if pr.return_against:
                    remote_pr_item = (
                        row.get("purchase_receipt_item")
                        or row.get("purchase_receipt_detail")
                        or row.get("name")
                    )
                    local_pr_item = get_pr_detail_for_item(
                        pr.return_against,
                        row.item_code,
                        remote_pr_item,
                    )
                    item_row["purchase_receipt_item"] = local_pr_item
                    item_row["purchase_receipt"] = pr.return_against
                else:
                    item_row["purchase_receipt_item"] = None
                    item_row["purchase_receipt"] = None

                # 7. Map external row identity
                if has_custom_remote_id:
                    item_row["custom_remote_id"] = row.name

                # 8. Resolve warehouse
                if not item_row.get("warehouse"):
                    item_row["warehouse"] = _resolve_default_warehouse(ext_pr.company)

                # Append ONLY ONCE
                pr.append("items", item_row)

            # If ALL items were skipped, don't create an empty PR
            if not pr.items:
                frappe.log_error(
                    title=f"PR Sync: No valid items for {remote_id}",
                    message=f"All items skipped because they don't exist in local Item Master."
                )
                frappe.db.set_value("External Purchase Receipt", name, "is_synced", 1, update_modified=False)
                results.append({"name": remote_id, "status": "skipped", "error": "No valid items"})
                continue

            # -----------------------------
            # TAXES & OTHER CHILD TABLES
            # -----------------------------
            _append_matching_child_table(pr, ext_pr, "taxes")
            for table_fieldname in ("pricing_rules", "supplied_items"):
                _append_matching_child_table(pr, ext_pr, table_fieldname)

            # -----------------------------
            # INSERT
            # -----------------------------
            _validate_or_clear_return_link(pr, ext_pr)
            _ensure_no_remote_ids_in_return_refs(pr)
            _safeguard_po_links(pr)
            _clear_invalid_company_address(pr)
            pr.set_posting_time = 1
            # Temporarily enable disabled items so validate() (which calls
            # validate_end_of_life via set_missing_item_details) doesn't throw
            # "Item X is disabled" during insert. Disabled items are re-disabled
            # immediately after by the context manager.
            with temporarily_enable_transaction_items(pr):
                pr.insert(ignore_permissions=True)
            clean_duplicate_pr_items(pr.name)

            # -----------------------------
            # NAMING SAFETY NET
            # -----------------------------
            final_name = pr.name
            if pr.name != target_name:
                if not frappe.db.exists("Purchase Receipt", target_name):
                    old_name = pr.name
                    frappe.db.sql("""UPDATE `tabPurchase Receipt` SET name = %s WHERE name = %s""", (target_name, old_name))
                    for df in pr_meta.get_table_fields():
                        child_table = df.options
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""UPDATE `tab{0}` SET parent = %s WHERE parent = %s""".format(child_table), (target_name, old_name))
                    final_name = target_name
                else:
                    frappe.log_error(
                        f"Purchase Receipt Sync: could not rename {pr.name} to {target_name} because that name already exists.",
                        "ExternalPurchaseReceipt Sync - name collision"
                    )

            # -----------------------------
            # DOCSTATUS SYNC
            # -----------------------------
            if source_docstatus == 2:
                # ORM cancel — reverses SLE + GL Entries properly.
                # Raw SQL cancel (_set_docstatus_directly) is intentionally disabled.
                # A newly inserted PR is always Draft (docstatus=0), so we must
                # submit first before cancel can proceed via the ORM.
                try:
                    _submit_pr_with_overflow_handling(final_name)
                    if not defer_source_cancellation:
                        pr_to_cancel = frappe.get_doc("Purchase Receipt", final_name)
                        with temporarily_enable_transaction_items(pr_to_cancel):
                            _safe_cancel_doc(pr_to_cancel, log_prefix=f"Purchase Receipt {final_name}")
                except Exception as cancel_err:
                    if strict:
                        # Pipeline mode: a failed cancel is a real failure.
                        # The savepoint rollback below keeps the record
                        # retryable (no last_synced_on stamp).
                        raise
                    if "NegativeStockError" in frappe.get_traceback():
                        pending_submit_retry.append(final_name)
                    frappe.log_error(
                        title=f"Purchase Receipt Cancel Error: {final_name}",
                        message=f"ORM submit/cancel failed: {frappe.get_traceback()}"
                    )
            elif source_docstatus == 1:
                try:
                    pr = _submit_pr_with_overflow_handling(final_name)
                except Exception as submit_error:
                    if strict:
                        raise
                    if "NegativeStockError" in frappe.get_traceback():
                        pending_submit_retry.append(final_name)
                    frappe.log_error(
                        title=f"Purchase Receipt Submit Error: {final_name}",
                        message=f"Source docstatus was 1 (Submitted) but submit failed: {frappe.get_traceback()}"
                    )

            if (
                cancel_return_parent_after_sync
                and local_return_against
                and frappe.utils.cint(
                    frappe.db.get_value("Purchase Receipt", final_name, "docstatus")
                ) == 2
            ):
                _cancel_submitted_return_parent(local_return_against, final_name)

            frappe.db.set_value("External Purchase Receipt", name, "is_synced", 1, update_modified=False)
            results.append({"name": final_name, "status": "synced"})

            frappe.db.set_value(
                "External Purchase Receipt",
                name,
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

        except Exception as e:
            try:
                if savepoint_created:
                    frappe.db.rollback(save_point=savepoint)
            except Exception:
                # A deadlock or nested rollback clears every savepoint. Do not
                # mask the original failure with "SAVEPOINT does not exist" or
                # return a partial batch as successful after its transaction
                # has already been rolled back.
                frappe.log_error(
                    title=f"Purchase Receipt Sync: savepoint unavailable for {name}",
                    message=frappe.get_traceback(),
                )
                raise e
            log_record_failure("Purchase Receipt", name, str(e))
            if strict:
                raise
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })
            break
        finally:
            release_sync_lock(_lock_name)

    # =========================================================================
    # RETRY PASS FOR RETURN ENTRIES DEFERRED BECAUSE PARENT WASN'T SYNCED YET
    # =========================================================================
    # These are PRs with is_return=1 whose return_against parent had not been
    # inserted when they were first encountered.  Now that the main loop has
    # finished, all parents that live in *this* chunk are present in the DB,
    # so a second pass can resolve them correctly.
    #
    # We pass _retry_pass=True so that, if the parent is still missing (it
    # lives in a future batch), the recursion does NOT defer again — it logs
    # and falls back instead, preventing infinite recursion.
    if pending_return_retry:
        retry_results = _sync_bulk_purchase_receipts(
            pending_return_retry, strict=strict, _retry_pass=True
        )
        # Replace the "deferred_return" placeholder entries with the real outcomes.
        deferred_names = set(pending_return_retry)
        results = [r for r in results if r.get("name") not in deferred_names]
        results.extend(retry_results)

    # =========================================================================
    # RETRY PASS FOR SUBMITS THAT FAILED ON NEGATIVE STOCK
    # =========================================================================
    for pr_name in pending_submit_retry:
        try:
            current_docstatus = frappe.db.get_value("Purchase Receipt", pr_name, "docstatus")
            if current_docstatus != 0:
                continue 

            doc = _submit_pr_with_overflow_handling(pr_name)

            for r in results:
                if r.get("name") == pr_name:
                    r["status"] = "synced (retry)"
                    break
        except Exception:
            frappe.log_error(
                title=f"Purchase Receipt Submit Retry Failed: {pr_name}",
                message=frappe.get_traceback()
            )
            break

    return results
