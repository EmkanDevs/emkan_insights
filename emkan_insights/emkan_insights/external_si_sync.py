import frappe
import json
from frappe.utils import flt
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.sync_guard import (
    acquire_sync_lock,
    release_sync_lock,
    set_source_site_fields,
    temporarily_enable_transaction_items,
)

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation",
    "quotation_item",
    "prevdoc_doctype",
    "prevdoc_docname",
    "so_detail",
    "against_sales_order",
    "si_detail",
    "sales_invoice_item",  # resolved explicitly below for return entries
    "name"
}

EXPLICITLY_HANDLED_HEADER_FIELDS = {
    "items", "taxes", "payment_schedule", "remote_id", "custom_remote_id",
    "customer", "return_against"
}

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300


def _sync_existing_docstatus(existing_name, ext_si):
    local_docstatus = frappe.db.get_value("Sales Invoice", existing_name, "docstatus")
    target_docstatus = int(ext_si.docstatus or 0)

    if int(local_docstatus or 0) == target_docstatus:
        return {"name": existing_name, "status": "exists", "docstatus": local_docstatus}

    si = frappe.get_doc("Sales Invoice", existing_name)
    si.flags.ignore_permissions = True

    if target_docstatus == 1 and int(local_docstatus or 0) == 0:
        with temporarily_enable_transaction_items(si):
            si.submit()
        return {"name": si.name, "status": "docstatus_synced", "docstatus": 1}

    if target_docstatus == 2:
        if int(local_docstatus or 0) == 0:
            with temporarily_enable_transaction_items(si):
                si.submit()
            si = frappe.get_doc("Sales Invoice", existing_name)
        with temporarily_enable_transaction_items(si):
            si.cancel()
        final_docstatus = frappe.db.get_value("Sales Invoice", existing_name, "docstatus")
        return {"name": existing_name, "status": "docstatus_synced", "docstatus": final_docstatus}

    return {
        "name": existing_name,
        "status": "exists",
        "docstatus": local_docstatus,
        "warning": "unhandled docstatus transition",
    }


# ==========================================================
# DUPLICATE CLEANUP
# ==========================================================

def clean_duplicate_si_items(parent_name):
    frappe.db.sql("""
        DELETE t1 FROM `tabSales Invoice Item` t1
        INNER JOIN `tabSales Invoice Item` t2
        WHERE
            t1.name > t2.name
            AND t1.parent = t2.parent
            AND t1.parent = %s
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
            AND IFNULL(t1.delivery_note, '') = IFNULL(t2.delivery_note, '')
            AND IFNULL(t1.sales_order, '') = IFNULL(t2.sales_order, '')
            AND IFNULL(t1.custom_remote_id, '') = IFNULL(t2.custom_remote_id, '')
            AND IFNULL(t1.custom_remote_id, '') <> ''
    """, (parent_name,))


def _dedupe_source_items(ext_items):
    seen = set()
    deduped = []
    for row in ext_items:
        row_id = row.name
        if row_id in seen:
            continue
        seen.add(row_id)
        deduped.append(row)
    return deduped


# ==========================================================
# LINK RESOLVERS
# ==========================================================

def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    if not remote_or_local_name:
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    meta = frappe.get_meta(doctype)
    for remote_field in ("custom_remote_id", "remote_id"):
        if meta.has_field(remote_field):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def get_child_detail(doctype, parent, item_code, remote_child_id=None):
    if not parent:
        return None

    if remote_child_id:
        match = frappe.db.get_value(
            doctype,
            {
                "parent": parent,
                "custom_remote_id": remote_child_id
            },
            "name"
        )

        if match:
            return match

    return frappe.db.get_value(
        doctype,
        {
            "parent": parent,
            "item_code": item_code
        },
        "name"
    )


def _resolve_customer(customer_ref, company_abbr):
    if not customer_ref:
        return None

    resolved = resolve_local_doc("Customer", customer_ref, company_abbr)
    if not resolved:
        frappe.log_error(
            title=f"Sales Invoice Sync - Customer '{customer_ref}' unresolved",
            message="Customer could not be resolved to a local Customer. Leaving field blank so the invoice still syncs."
        )
    return resolved


def _resolve_default_warehouse(company):
    if not company:
        return None
    return frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")


def _ensure_item_uom(item_code, uom):
    if not item_code or not uom:
        return

    if not frappe.db.exists("UOM", uom):
        return

    already_registered = frappe.db.exists("UOM Conversion Detail", {
        "parent": item_code,
        "parenttype": "Item",
        "uom": uom
    })
    if already_registered:
        return

    try:
        frappe.get_doc({
            "doctype": "UOM Conversion Detail",
            "parent": item_code,
            "parenttype": "Item",
            "parentfield": "uoms",
            "uom": uom,
            "conversion_factor": 1.0
        }).insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(
            title=f"Failed to auto-register UOM {uom} on Item {item_code}",
            message=str(e)
        )


# ==========================================================
# COST CENTER RESOLVER  ← THE KEY FIX
# ==========================================================

def _resolve_cost_center(cost_center, company, company_abbr, cc_cache):
    """
    Resolve a cost center from the source document to a valid local one.

    Handles every variant seen in the wild:
      1. Exact match                            → use as-is
      2. Double company suffix "X - IMC - IMC"  → strip to "X - IMC"
      3. Missing company suffix "X"             → append " - IMC"
      4. Wrong company suffix "X - OTHER"       → try without suffix
      5. Nothing matches                        → fall back to company default

    All lookups are cached in cc_cache to avoid repeated DB hits
    when the same cost center appears on many item rows.
    """
    if not cost_center:
        return None

    # Check cache first
    if cost_center in cc_cache:
        return cc_cache[cost_center]

    result = None
    single_suffix = f" - {company_abbr}" if company_abbr else ""
    double_suffix = f"{single_suffix}{single_suffix}" if company_abbr else ""

    # --- Tier 1: Exact match ---
    if frappe.db.exists("Cost Center", cost_center):
        result = cost_center

    # --- Tier 2: Fix double suffix "X - IMC - IMC" → "X - IMC" ---
    if not result and double_suffix and cost_center.endswith(double_suffix):
        candidate = cost_center[: -len(double_suffix)] + single_suffix
        if frappe.db.exists("Cost Center", candidate):
            result = candidate

    # --- Tier 3: Strip single suffix "X - IMC" → "X" ---
    if not result and single_suffix and cost_center.endswith(single_suffix):
        candidate = cost_center[: -len(single_suffix)]
        if frappe.db.exists("Cost Center", candidate):
            result = candidate

    # --- Tier 4: Add single suffix "X" → "X - IMC" ---
    if not result and single_suffix and not cost_center.endswith(single_suffix):
        candidate = cost_center + single_suffix
        if frappe.db.exists("Cost Center", candidate):
            result = candidate

    # --- Tier 5: Company default cost center ---
    if not result and company:
        default_cc = frappe.db.get_value("Company", company, "cost_center")
        if default_cc and frappe.db.exists("Cost Center", default_cc):
            result = default_cc

    cc_cache[cost_center] = result
    return result


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_sales_invoice_docs(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"si_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_si_sync._sync_sales_invoice_docs_job",
            queue="long",
            job_name=job_name,
            timeout=6000,
            source_doctype=source_doctype,
            names=chunk,
        )
        queued_chunks += 1

    return (
        f"Sync Queued: {len(unique_names)} record(s) split into "
        f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def _sync_sales_invoice_docs_job(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    # Each background job gets its own cc_cache — perfectly safe since
    # each job runs in its own worker process with its own DB connection.
    cc_cache = {}

    for name in names:
        lock_name = None
        savepoint = None
        try:
            lock_name = acquire_sync_lock(source_doctype, name)
            savepoint = frappe.scrub(f"sync_si_{name}")[:50]
            frappe.db.savepoint(savepoint)

            ext_si = frappe.get_doc(source_doctype, name)
            sync_started_at = ext_si.modified
            remote_id = ext_si.name

            # ------------------------------------------------
            # BUILD TARGET NAME
            # ------------------------------------------------
            company_abbr = frappe.db.get_value('Company', ext_si.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Sales Invoice Sync: no company abbr found for company "
                    f"'{ext_si.company}' on {remote_id}.",
                    "ExternalSalesInvoice Sync - missing company abbr"
                )

            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # Dynamic remote_id field detection
            # ------------------------------------------------
            remote_id_field = None
            si_meta = frappe.get_meta("Sales Invoice")
            for candidate in ("custom_remote_id", "remote_id"):
                if si_meta.has_field(candidate):
                    remote_id_field = candidate
                    break

            if not remote_id_field:
                frappe.throw(
                    "Sales Invoice has neither 'custom_remote_id' nor "
                    "'remote_id' field - cannot track sync identity."
                )

            # ------------------------------------------------
            # Check existing
            # ------------------------------------------------
            existing_si = frappe.db.get_value(
                "Sales Invoice",
                {remote_id_field: remote_id},
                "name"
            ) or (target_name if frappe.db.exists("Sales Invoice", target_name) else None)

            if existing_si:
                result = _sync_existing_docstatus(existing_si, ext_si)
                if result.get("status") in ("exists", "docstatus_synced"):
                    ext_si.db_set(
                        "last_synced_on",
                        sync_started_at,
                        update_modified=False
                    )
                results.append(result)
                continue

            si = frappe.new_doc("Sales Invoice")

            si.set(remote_id_field, remote_id)
            si.name = target_name
            si.flags.name_set = True

            set_source_site_fields(si, site_url=getattr(ext_si, "source_site", None))

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # ------------------------------------------------
            for field, value in ext_si.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in EXPLICITLY_HANDLED_HEADER_FIELDS
                    and hasattr(si, field)
                ):
                    si.set(field, value)

            # Preserve source posting/due dates. Without set_posting_time=1,
            # ERPNext's validate_posting_time() overwrites posting_date to
            # today, which then fails: due_date < posting_date.
            si.posting_date = ext_si.posting_date
            if ext_si.get("posting_time"):
                si.posting_time = ext_si.posting_time
            if ext_si.get("due_date"):
                si.due_date = ext_si.due_date
            si.set_posting_time = 1

            si.customer = _resolve_customer(ext_si.get("customer"), company_abbr)

            # ------------------------------------------------
            # RESOLVE RETURN AGAINST
            # ------------------------------------------------
            if ext_si.get("return_against"):
                local_return_against = resolve_local_doc("Sales Invoice", ext_si.return_against, company_abbr)
                if local_return_against:
                    si.return_against = local_return_against
                else:
                    frappe.log_error(
                        title=f"Sales Invoice Sync - return_against '{ext_si.return_against}' unresolved",
                        message="Return against SI could not be resolved. Leaving blank."
                    )
            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
            si.set("items", [])
            deduped_items = _dedupe_source_items(ext_si.items)

            for row in deduped_items:
                item_row = {}

                for field, value in row.as_dict().items():
                    if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                        item_row[field] = value

                # Resolve Sales Order
                if row.get("sales_order"):
                    local_so = resolve_local_doc("Sales Order", row.sales_order, company_abbr)
                    item_row["sales_order"] = local_so
                    item_row["so_detail"] = get_child_detail(
                        "Sales Order Item",
                        local_so,
                        row.item_code,
                        remote_child_id=row.get("so_detail")
                    ) if local_so else None
                else:
                    item_row["sales_order"] = None
                    item_row["so_detail"] = None

                # Resolve Delivery Note
                if row.get("delivery_note"):
                    local_dn = resolve_local_doc("Delivery Note", row.delivery_note, company_abbr)
                    item_row["delivery_note"] = local_dn
                    item_row["dn_detail"] = get_child_detail(
                        "Delivery Note Item",
                        local_dn,
                        row.item_code,
                        remote_child_id=row.get("dn_detail")
                    ) if local_dn else None
                else:
                    item_row["delivery_note"] = None
                    item_row["dn_detail"] = None

                # Resolve Purchase Order
                if row.get("purchase_order"):
                    local_po = resolve_local_doc("Purchase Order", row.purchase_order, company_abbr)
                    item_row["purchase_order"] = local_po
                    item_row["po_detail"] = get_child_detail(
                        "Purchase Order Item",
                        local_po,
                        row.item_code,
                        remote_child_id=row.get("po_detail")
                    ) if local_po else None
                else:
                    item_row["purchase_order"] = None
                    item_row["po_detail"] = None

                # Resolve SI Detail (Return Invoice)
                if si.return_against and row.get("si_detail"):
                    item_row["si_detail"] = get_child_detail(
                        "Sales Invoice Item",
                        si.return_against,
                        row.item_code,
                        remote_child_id=row.get("si_detail")
                    )
                else:
                    item_row["si_detail"] = None

                # Resolve sales_invoice_item (required for return invoice validation).
                #
                # ERPNext's validate_returned_items() checks:
                #   key = (item_code, sales_invoice_item)
                # and that key must exist in the *local* return_against SI's items
                # (keyed by their local `name`).
                #
                # On the source system, items[i].sales_invoice_item holds the remote
                # row name (e.g. "SV-PR-LR-RM-00003"), which does NOT match the local
                # row name.  We resolve it here via custom_remote_id.
                if si.return_against:
                    remote_sii = row.get("sales_invoice_item") or row.get("name")
                    local_sii = None
                    if remote_sii:
                        # 1. Match by custom_remote_id on the local return_against SI
                        local_sii = frappe.db.get_value(
                            "Sales Invoice Item",
                            {"parent": si.return_against, "custom_remote_id": remote_sii},
                            "name"
                        )
                        # 2. Fallback: match by item_code if only one row has that item
                        if not local_sii:
                            local_sii = frappe.db.get_value(
                                "Sales Invoice Item",
                                {"parent": si.return_against, "item_code": row.item_code},
                                "name"
                            )
                    item_row["sales_invoice_item"] = local_sii
                else:
                    item_row["sales_invoice_item"] = None

                # Map external row identity
                if frappe.get_meta("Sales Invoice Item").has_field("custom_remote_id"):
                    item_row["custom_remote_id"] = row.name

                # Resolve warehouse
                if not item_row.get("warehouse"):
                    item_row["warehouse"] = _resolve_default_warehouse(ext_si.company)

                # Auto-register UOM
                if item_row.get("item_code") and item_row.get("uom"):
                    _ensure_item_uom(item_row["item_code"], item_row["uom"])

                # Append ONLY ONCE
                si.append("items", item_row)

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_si, "taxes"):
                si.set("taxes", [])
                for row in ext_si.taxes:
                    tax_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value
                    if tax_row.get("account_head"):
                        tax_row["account_head"] = resolve_company_account(
                            tax_row["account_head"], ext_si.company, company_abbr
                        )
                    si.append("taxes", tax_row)

            # ------------------------------------------------
            # PAYMENT SCHEDULE
            # ------------------------------------------------
            if hasattr(ext_si, "payment_schedule"):
                si.set("payment_schedule", [])
                for row in ext_si.payment_schedule:
                    schedule_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS:
                            schedule_row[field] = value
                    si.append("payment_schedule", schedule_row)

            # ============================================================
            # FIX 1 — COST CENTER RESOLUTION  (THE CRITICAL FIX)
            # ============================================================
            #
            # Source documents carry cost centers like:
            #   "PV0023 RPO#... - PETRO-V LEAK AND SEAL NON METALLIC - IMC"
            #   "PV003 ... - PETRO-V ... - IMC - IMC"   ← double suffix
            #
            # Many of these do NOT exist in the local system.
            # ERPNext's make_gl_entries() silently drops any row whose
            # cost_center is invalid — that is your missing 18 GL entries.
            #
            # Resolution strategy (per item row):
            #   1. Try exact match
            #   2. Fix double suffix "X - IMC - IMC" → "X - IMC"
            #   3. Try without suffix "X - IMC" → "X"
            #   4. Try adding suffix "X" → "X - IMC"
            #   5. Fall back to company default cost center
            #   6. If STILL nothing, clear to None so that
            #      set_missing_values() picks up the Item's
            #      default_cost_center as last resort.
            #
            # Same logic applied to tax rows (they also generate GL
            # entries and also carry cost_center).
            # ============================================================

            for item in si.items:
                if item.cost_center:
                    original_cc = item.cost_center
                    resolved_cc = _resolve_cost_center(
                        original_cc, ext_si.company, company_abbr, cc_cache
                    )
                    if resolved_cc:
                        item.cost_center = resolved_cc
                    else:
                        frappe.log_error(
                            title=f"SI Sync: Cost Center unresolved — {remote_id}",
                            message=(
                                f"Item: {item.item_code}\n"
                                f"Source Cost Center: {original_cc}\n"
                                f"Could not resolve to any local Cost Center. "
                                f"Clearing so set_missing_values() uses item default."
                            )
                        )
                        item.cost_center = None

            for tax in si.taxes:
                if tax.cost_center:
                    original_cc = tax.cost_center
                    resolved_cc = _resolve_cost_center(
                        original_cc, ext_si.company, company_abbr, cc_cache
                    )
                    if resolved_cc:
                        tax.cost_center = resolved_cc
                    else:
                        frappe.log_error(
                            title=f"SI Sync: Tax Cost Center unresolved — {remote_id}",
                            message=(
                                f"Tax: {getattr(tax, 'description', '') or getattr(tax, 'account_head', '')}\n"
                                f"Source Cost Center: {original_cc}\n"
                                f"Could not resolve. Clearing cost center on tax row."
                            )
                        )
                        tax.cost_center = None

            # ============================================================
            # FIX 2 — INCOME / EXPENSE ACCOUNT RESOLUTION
            # ============================================================
            # Accounts copied from source may not exist locally.
            # Clear invalid ones so set_missing_values() assigns the
            # correct local defaults from the Item master.
            # ============================================================

            for item in si.items:
                if item.income_account:
                    item.income_account = resolve_company_account(
                        item.income_account, ext_si.company, company_abbr
                    )
                if item.income_account and not frappe.db.exists("Account", item.income_account):
                    item.income_account = None
                if item.expense_account:
                    item.expense_account = resolve_company_account(
                        item.expense_account, ext_si.company, company_abbr
                    )
                if item.expense_account and not frappe.db.exists("Account", item.expense_account):
                    item.expense_account = None

            # ============================================================
            # FIX 3 — CALCULATE TOTALS FOR GL ENTRY GENERATION
            # ============================================================
            # set_missing_values(): fills income_account, expense_account,
            #   cost_center (if still None), item_wise_tax_detail
            # calculate_taxes_and_totals(): fills base_net_amount,
            #   base_tax_amount_after_discount_amount,
            #   item.item_wise_tax_detail, all header totals
            # ALL of these are REQUIRED by make_gl_entries().
            # ============================================================

            si.set_missing_values()
            si.calculate_taxes_and_totals()

            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            si.merge_similar_items = 0
            with temporarily_enable_transaction_items(si):
                si.insert(
                    ignore_permissions=True,
                )

            clean_duplicate_si_items(si.name)

            # ------------------------------------------------
            # NAMING SAFETY NET
            # ------------------------------------------------
            if si.name != target_name:
                if not frappe.db.exists("Sales Invoice", target_name):
                    old_name = si.name

                    frappe.db.sql("""
                        UPDATE `tabSales Invoice`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, old_name))

                    for child_table in [
                        "Sales Invoice Item",
                        "Sales Taxes and Charges",
                        "Payment Schedule"
                    ]:
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""
                                UPDATE `tab{0}`
                                SET parent = %s
                                WHERE parent = %s
                            """.format(child_table), (target_name, old_name))

                    si.name = target_name

                else:
                    frappe.log_error(
                        f"Sales Invoice Sync: could not rename {si.name} to "
                        f"{target_name} because that name already exists.",
                        "ExternalSalesInvoice Sync - name collision"
                    )

            # ------------------------------------------------
            # DOCSTATUS SYNC
            # ------------------------------------------------
            if ext_si.docstatus == 1:
                with temporarily_enable_transaction_items(si):
                    si.submit()

            elif ext_si.docstatus == 2:
                with temporarily_enable_transaction_items(si):
                    si.submit()
                    si.cancel()

            results.append({
                "name": si.name,
                "status": "synced"
            })

            ext_si.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

        except Exception as e:
            if savepoint:
                frappe.db.rollback(save_point=savepoint)
            error = frappe.get_traceback()

            frappe.log_error(
                title=f"Sales Invoice Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })
            break
        finally:
            release_sync_lock(lock_name)

    return results
