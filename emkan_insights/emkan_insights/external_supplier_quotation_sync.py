import frappe
import json
from frappe.utils import cint, flt
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import (
    set_source_site_fields,
    sync_record_guard,
    temporarily_enable_transaction_items,
)
from emkan_insights.emkan_insights.uom_resolver import resolve_uom

# ⭐ NEW: Chunk size for splitting large bulk syncs into multiple background jobs.
CHUNK_SIZE = 300

# ==========================================================
# ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_supplier_quotation_docs(source_doctype: str = "External Supplier Quotation", names=None, sync_now: bool = False):
    if isinstance(names, str):
        names = json.loads(names)

    if not names:
        all_names = frappe.get_all(
            "External Supplier Quotation",
            pluck="name",
            limit_page_length=0
        )
    else:
        all_names = list(names)

    if not all_names:
        return "No External Supplier Quotations found to sync"

    # De-duplicate incoming names while preserving order. This guarantees
    # the same record can never end up in two different chunks, which is the
    # only scenario that could cause a duplicate Supplier Quotation to be created
    # when chunks run in parallel on different workers.
    seen = set()
    unique_names = []
    for n in all_names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    # When called from the sequential scheduler (sync_now=True), run inline so
    # the pipeline waits for completion before moving on to the next doctype.
    # When called directly from the UI or other callers, enqueue as usual so
    # the request returns immediately.
    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: sync_bulk_supplier_quotations(chunk),
        )

    job_name = f"sq_sync_chunk_{frappe.generate_hash(length=8)}_0"
    frappe.enqueue(
        "emkan_insights.emkan_insights.external_supplier_quotation_sync._sync_supplier_quotation_docs_job",
        queue="long",
        job_name=job_name,
        timeout=3000,
        source_doctype=source_doctype,
        names=unique_names,
        batch_index=0,
    )

    return (
        f"Sync Queued: {len(unique_names)} record(s) in sequential "
        f"batches of up to {CHUNK_SIZE} each"
    )


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def _sync_supplier_quotation_docs_job(source_doctype=None, names=None, batch_index=0):
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    batch_index = int(batch_index or 0)
    chunk = names[batch_index * CHUNK_SIZE:(batch_index + 1) * CHUNK_SIZE]
    result = sync_bulk_supplier_quotations(chunk)

    next_batch_index = batch_index + 1
    if not result.get("failed") and next_batch_index * CHUNK_SIZE < len(names):
        job_name = f"sq_sync_chunk_{frappe.generate_hash(length=8)}_{next_batch_index}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_supplier_quotation_sync._sync_supplier_quotation_docs_job",
            queue="long",
            job_name=job_name,
            timeout=3000,
            source_doctype=source_doctype,
            names=names,
            batch_index=next_batch_index,
        )

    return result


# ==========================================================
# BULK SYNC
# ==========================================================

def sync_bulk_supplier_quotations(names):
    """
    Processed in background queue.
    
    All database writes happen within a single transaction.
    Frappe automatically commits the transaction when the background job finishes.
    """

    sources = frappe.get_all(
        "External Supplier Quotation",
        filters={"name": ["in", names]},
        fields=["name", "remote_id", "company"],
        limit_page_length=0
    )

    full_docs = {}
    for d in sources:
        try:
            full_docs[d.name] = frappe.get_doc("External Supplier Quotation", d.name)
        except Exception as e:
            frappe.log_error(f"Failed to load External SQ {d.name}: {str(e)}", "SQ Sync")
            raise

    suppliers  = set(frappe.get_all("Supplier",  pluck="name", limit_page_length=0))
    items      = set(frappe.get_all("Item",      pluck="name", limit_page_length=0))
    warehouses = set(frappe.get_all("Warehouse", pluck="name", limit_page_length=0))

    remote_ids = [d.remote_id for d in sources if d.remote_id]
    company_abbr_map = {d.name: frappe.db.get_value("Company", d.company, "abbr") or "EXT" for d in sources}

    existing_sq = []
    if remote_ids:
        # PRIORITY 1: Check by remote_id field on Supplier Quotation
        existing_sq += frappe.get_all(
            "Supplier Quotation",
            filters={"remote_id": ["in", remote_ids]},
            fields=["name", "remote_id", "docstatus"],
            limit_page_length=0
        )

        # PRIORITY 2: Check by custom_remote_id field
        try:
            existing_sq += frappe.get_all(
                "Supplier Quotation",
                filters={"custom_remote_id": ["in", remote_ids]},
                fields=["name", "custom_remote_id as remote_id", "docstatus"],
                limit_page_length=0
            )
        except Exception:
            pass

        # PRIORITY 3: Check by name pattern
        for d in sources:
            if d.remote_id:
                abbr = company_abbr_map.get(d.name, "EXT")
                if d.remote_id.startswith(f"{abbr}-"):
                    expected_name = d.remote_id
                else:
                    expected_name = f"{abbr}-{d.remote_id}"
                if frappe.db.exists("Supplier Quotation", expected_name):
                    existing_sq.append({
                        "name": expected_name,
                        "remote_id": d.remote_id,
                        "docstatus": frappe.db.get_value("Supplier Quotation", expected_name, "docstatus") or 0
                    })

    # FIX: Handle dict access properly
    existing_map = {}
    for d in existing_sq:
        rid = d.get("remote_id") if isinstance(d, dict) else getattr(d, "remote_id", None)
        if rid:
            existing_map[rid] = d

    success = 0

    for idx, name in enumerate(names, start=1):
        savepoint = f"sq_sync_{frappe.generate_hash(length=10)}"
        src = full_docs.get(name)

        try:
            with sync_record_guard("External Supplier Quotation", name):
                frappe.db.savepoint(savepoint)

                if not src:
                    raise Exception(f"Source doc not found: {name}")

                if not src.remote_id:
                    raise Exception("remote_id is missing")

                if not src.company:
                    raise Exception("Company is missing")

                if not src.supplier:
                    raise Exception("Supplier is missing")

                sync_started_at = src.modified

                existing = existing_map.get(src.remote_id)

                doc = upsert_supplier_quotation(
                    src,
                    existing_name=existing["name"] if existing else None,
                    suppliers=suppliers,
                    items=items,
                    warehouses=warehouses
                )

                sync_docstatus(doc, int(src.docstatus or 0))

                src.db_set(
                    "last_synced_on",
                    sync_started_at,
                    update_modified=False
                )

                success += 1

        except Exception as e:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
            log_record_failure("Supplier Quotation", name, str(e))
            raise

        # Frappe owns the transaction boundary for this job.

    return {"success": success}


# ==========================================================
# ITEM / SUPPLIER / WAREHOUSE RESOLVERS
# ==========================================================

def _resolve_item_code(row_item_code, row_item_name=None, row_uom=None, company_abbr=None):
    """Resolve External Item code to local Item code with leading-zero fix."""
    if not row_item_code:
        return None

    variants = [row_item_code]
    if not row_item_code.startswith("0"):
        variants.append("0" + row_item_code)
    if row_item_code.startswith("0") and len(row_item_code) > 1:
        variants.append(row_item_code.lstrip("0"))
    if company_abbr:
        variants = [f"{company_abbr}-{variant}" for variant in variants] + variants

    for variant in variants:
        ext_item = frappe.db.get_value("External Item", variant,
            ["name", "remote_id", "item_code"], as_dict=1)
        if ext_item and ext_item.remote_id:
            if frappe.db.exists("Item", ext_item.remote_id):
                return ext_item.remote_id

    for variant in variants:
        ext_item = frappe.db.get_value("External Item", {"item_code": variant},
            ["name", "remote_id", "item_code"], as_dict=1)
        if ext_item and ext_item.remote_id:
            if frappe.db.exists("Item", ext_item.remote_id):
                return ext_item.remote_id

    # FIX: fall back to Item.custom_remote_id directly. The Item sync
    # (external_item_sync.py) always sets custom_remote_id = External Item's
    # own name, but only backfills External Item.remote_id when it happened
    # to already be set beforehand. So any Item synced without a pre-existing
    # remote_id has no reverse link via "External Item.remote_id" above, and
    # would otherwise be missed here even though a valid mapping exists.
    for variant in variants:
        item_name = frappe.db.get_value("Item", {"custom_remote_id": variant}, "name")
        if item_name:
            return item_name

    if frappe.db.exists("Item", row_item_code):
        return row_item_code

    # FALLBACK: Auto-create from External Item
    ext_item = frappe.db.get_value("External Item", row_item_code,
        ["name", "item_code", "item_name", "stock_uom", "item_group", "remote_id"], as_dict=1)

    if ext_item:
        created = _auto_create_item(ext_item, company_abbr=company_abbr)
        if created:
            return created
        return ext_item.remote_id or ext_item.item_code or ext_item.name

    # FINAL FALLBACK: Create Item directly from SQ row data
    if row_item_code and not frappe.db.exists("Item", row_item_code):
        try:
            item = frappe.new_doc("Item")
            item.item_code = row_item_code
            item.item_name = row_item_name or row_item_code
            prefixed_item_group = f"{company_abbr}-All Item Groups" if company_abbr else None
            item.item_group = (
                prefixed_item_group
                if prefixed_item_group and frappe.db.exists("Item Group", prefixed_item_group)
                else "All Item Groups"
            )
            item.stock_uom = row_uom or "Nos"
            item.is_stock_item = 1
            item.flags.ignore_permissions = True
            item.insert(ignore_permissions=True)
            return row_item_code
        except Exception as e:
            frappe.log_error(f"Direct Item create failed for {row_item_code}: {str(e)}", "SQ Sync")

    return row_item_code


def _resolve_supplier(supplier_code):
    """Resolve External Supplier to local Supplier."""
    if not supplier_code:
        return None

    ext_sup = frappe.db.get_value("External Supplier", supplier_code,
        ["name", "remote_id"], as_dict=1)
    if ext_sup and ext_sup.remote_id:
        return ext_sup.remote_id

    ext_sup = frappe.db.get_value("External Supplier", {"supplier_name": supplier_code},
        ["name", "remote_id"], as_dict=1)
    if ext_sup and ext_sup.remote_id:
        return ext_sup.remote_id

    if frappe.db.exists("Supplier", supplier_code):
        return supplier_code

    return None


def _enable_supplier_for_sync(supplier):
    state = frappe.db.get_value(
        "Supplier",
        supplier,
        ["disabled", "is_frozen"],
        as_dict=True,
    )
    if not state:
        return False

    updates = {}
    if cint(state.get("disabled")) == 1:
        updates["disabled"] = 0
    if cint(state.get("is_frozen")) == 1:
        updates["is_frozen"] = 0
    if not updates:
        return False

    frappe.db.set_value("Supplier", supplier, updates)
    frappe.clear_document_cache("Supplier", supplier)
    return True


def _auto_create_item(ext_item, company_abbr=None):
    """Create local Item from External Item data."""
    if not ext_item:
        return None

    item_code = ext_item.remote_id or ext_item.item_code or ext_item.name

    if frappe.db.exists("Item", item_code):
        return item_code

    try:
        item = frappe.new_doc("Item")
        item.item_code = item_code
        item.item_name = ext_item.get("item_name") or item_code
        item_group = ext_item.get("item_group") or "All Item Groups"
        prefixed_item_group = f"{company_abbr}-{item_group}" if company_abbr else None
        item.item_group = (
            prefixed_item_group
            if prefixed_item_group and frappe.db.exists("Item Group", prefixed_item_group)
            else item_group
        )
        item.stock_uom = ext_item.get("stock_uom") or "Nos"
        item.is_stock_item = 1
        item.flags.ignore_permissions = True
        item.insert(ignore_permissions=True)
        
        return item_code
    except Exception as e:
        frappe.log_error(f"Auto-create Item failed for {item_code}: {str(e)}", "SQ Sync - Auto Create Item")
        return None


def _resolve_warehouse(warehouse_code, company):
    """Resolve External Warehouse to local Warehouse."""
    if not warehouse_code:
        return None

    ext_wh = frappe.db.get_value("External Warehouse", warehouse_code,
        ["name", "remote_id"], as_dict=1)
    if ext_wh and ext_wh.remote_id:
        return ext_wh.remote_id

    if frappe.db.exists("Warehouse", warehouse_code):
        return warehouse_code

    return frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")


def _ensure_item_uom(item_code, uom):
    """
    ERPNext throws 'UOM {uom} not found in Item {item_code}' if a transaction
    row uses a UOM that isn't registered in that Item's UOM conversion table.
    Instead of failing the whole Supplier Quotation, add the missing UOM
    to the Item directly (conversion_factor 1, same default the Item sync
    uses for stock_uom) so the row saves cleanly.
    """
    if not item_code or not uom:
        return

    if not frappe.db.exists("UOM", uom):
        # Don't try to register a UOM that doesn't exist at all — that's a
        # separate data issue and should surface as its own error.
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
# LINK RESOLVER
# ==========================================================

def _resolve_link_with_abbr(doctype, remote_name, company_abbr=None):
    if not remote_name:
        return None

    all_abbrs = frappe.db.sql_list("SELECT DISTINCT abbr FROM tabCompany WHERE abbr IS NOT NULL")

    base_name = remote_name
    for abbr in all_abbrs:
        prefix = f"{abbr}-"
        if base_name.startswith(prefix):
            base_name = base_name[len(prefix):]
            break

    candidates = []
    if remote_name:
        candidates.append(remote_name)
    if company_abbr and base_name:
        candidates.append(f"{company_abbr}-{base_name}")
    # Always try company_abbr + full remote name (covers double-prefix
    # internal RFQs: External IMC-RFQ-… → Internal IMC-IMC-RFQ-…).
    if company_abbr and remote_name and not remote_name.startswith(f"{company_abbr}-{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_name}")

    seen = set()
    unique_candidates = []
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            unique_candidates.append(name)

    for name in unique_candidates:
        if frappe.db.exists(doctype, name):
            return name

    meta = frappe.get_meta(doctype)
    for name in unique_candidates:
        for field in ("remote_id", "custom_remote_id"):
            if meta.has_field(field):
                local_name = frappe.db.get_value(doctype, {field: name}, "name")
                if local_name:
                    return local_name

    # Bridge via External Request for Quotation: External SQ items store the
    # External RFQ name; synced RFQ sets custom_remote_id = that External name.
    if doctype == "Request for Quotation":
        for name in unique_candidates:
            if frappe.db.exists("External Request for Quotation", name):
                linked = frappe.db.get_value(
                    "External Request for Quotation", name, "remote_id"
                )
                if linked and frappe.db.exists("Request for Quotation", linked):
                    return linked
                if meta.has_field("custom_remote_id"):
                    local_name = frappe.db.get_value(
                        "Request for Quotation", {"custom_remote_id": name}, "name"
                    )
                    if local_name:
                        return local_name

    return None


def _resolve_child_detail(child_doctype, parent, item_code, remote_item_name=None):
    """Resolve a child row on a linked parent (RFQ Item / MR Item, etc.)."""
    if not parent:
        return None

    if remote_item_name and frappe.get_meta(child_doctype).has_field("custom_remote_id"):
        exact = frappe.db.get_value(
            child_doctype,
            {"parent": parent, "custom_remote_id": remote_item_name},
            "name",
        )
        if exact:
            return exact

    if item_code:
        return frappe.db.get_value(
            child_doctype,
            {"parent": parent, "item_code": item_code},
            "name",
        )

    return remote_item_name


def _build_item_reference_fields(row, company_abbr, resolved_item):
    """Map Material Request / RFQ / SO / PO links from External SQ item."""
    refs = {}

    if row.get("material_request"):
        mr = _resolve_link_with_abbr(
            "Material Request", row.material_request, company_abbr
        )
        if mr:
            # Drop the link if the MR is cancelled — ERPNext raises
            # CancelledLinkError on submit when any linked doc is docstatus=2.
            mr_docstatus = frappe.db.get_value("Material Request", mr, "docstatus")
            if frappe.utils.cint(mr_docstatus) == 2:
                frappe.log_error(
                    title=f"SQ item: MR link cleared (MR cancelled)",
                    message=(
                        f"Material Request '{mr}' is cancelled (docstatus=2). "
                        f"Clearing the link so the SQ can be submitted without "
                        f"CancelledLinkError."
                    ),
                )
                mr = None
        refs["material_request"] = mr
        refs["material_request_item"] = _resolve_child_detail(
            "Material Request Item",
            mr,
            resolved_item,
            remote_item_name=row.get("material_request_item"),
        ) if mr else None

    if row.get("request_for_quotation"):
        rfq = _resolve_link_with_abbr(
            "Request for Quotation", row.request_for_quotation, company_abbr
        )
        refs["request_for_quotation"] = rfq
        refs["request_for_quotation_item"] = _resolve_child_detail(
            "Request for Quotation Item",
            rfq,
            resolved_item,
            remote_item_name=row.get("request_for_quotation_item"),
        ) if rfq else row.get("request_for_quotation_item")

    if row.get("sales_order"):
        so = _resolve_link_with_abbr("Sales Order", row.sales_order, company_abbr)
        refs["sales_order"] = so
        refs["sales_order_item"] = row.get("sales_order_item")

    if row.get("purchase_order"):
        po = _resolve_link_with_abbr(
            "Purchase Order", row.purchase_order, company_abbr
        )
        refs["purchase_order"] = po
        refs["purchase_order_item"] = row.get("purchase_order_item")

    return refs


# ==========================================================
# UPSERT
# ==========================================================

def upsert_supplier_quotation(src, existing_name=None, suppliers=None, items=None, warehouses=None):

    if not src.remote_id:
        raise Exception(f"External SQTN {src.name} has no remote_id")

    company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""

    # Don't double-prefix if remote_id already starts with company_abbr
    if company_abbr and src.remote_id.startswith(f"{company_abbr}-"):
        target_name = src.remote_id
    else:
        target_name = f"{company_abbr}-{src.remote_id}" if company_abbr else src.remote_id

    resolved_supplier = _resolve_supplier(src.supplier)
    if not resolved_supplier:
        raise Exception(f"Supplier '{src.supplier}' could not be resolved to local Supplier")
    _enable_supplier_for_sync(resolved_supplier)

    # ----------------------------------------------------------
    # FIX: if the caller didn't resolve existing_name (lookup by
    # remote_id/custom_remote_id missed it) but a Supplier Quotation
    # already exists under target_name, treat this as an UPDATE
    # instead of silently returning the untouched existing doc.
    # This was the cause of records reporting "success" while never
    # actually being synced.
    # ----------------------------------------------------------
    if not existing_name and frappe.db.exists("Supplier Quotation", target_name):
        existing_name = target_name

    # ----------------------------------------------------------
    # LOAD OR CREATE
    # ----------------------------------------------------------
    if existing_name:
        doc = frappe.get_doc("Supplier Quotation", existing_name)

        if doc.docstatus in (1, 2):
            # Document is already submitted or cancelled. ERPNext does not allow
            # editing a submitted/cancelled document's items via ORM or SQL without
            # going through the cancel+amend lifecycle, which would break all
            # downstream links (PO items, etc.).
            #
            # Safe approach: update only non-structural header fields via db_set,
            # then let sync_docstatus() handle any docstatus transition.
            # Item re-sync is skipped for submitted/cancelled docs.
            update_fields = {
                "transaction_date": src.transaction_date,
                "valid_till": src.valid_till,
                "currency": src.currency,
                "conversion_rate": src.conversion_rate or 1,
            }
            if frappe.get_meta("Supplier Quotation").has_field("custom_remote_id"):
                update_fields["custom_remote_id"] = src.remote_id
            elif hasattr(doc, "remote_id"):
                update_fields["remote_id"] = src.remote_id
            if hasattr(doc, "source_site") and src.get("source_site"):
                update_fields["source_site"] = src.source_site
            frappe.db.set_value("Supplier Quotation", doc.name, update_fields, update_modified=False)
            frappe.clear_document_cache("Supplier Quotation", doc.name)
            doc.reload()
            # Skip item/tax re-sync for submitted/cancelled docs and return early.
            # sync_docstatus() in the caller will handle any docstatus transition.
            return doc

        doc.set("items", [])
        doc.set("taxes", [])

    else:
        doc = frappe.new_doc("Supplier Quotation")
        doc.name = target_name
        doc.flags.name_set = True

    # ----------------------------------------------------------
    # HEADER FIELDS
    # ----------------------------------------------------------
    doc.company           = src.company
    doc.supplier          = resolved_supplier
    doc.transaction_date  = src.transaction_date
    doc.valid_till        = src.valid_till
    doc.currency          = src.currency
    doc.buying_price_list = src.buying_price_list
    doc.conversion_rate   = src.conversion_rate or 1

    if frappe.get_meta("Supplier Quotation").has_field("custom_remote_id"):
        doc.custom_remote_id = src.remote_id
    elif hasattr(doc, "remote_id"):
        doc.remote_id = src.remote_id

    set_source_site_fields(doc, site_url=src.get("source_site"))

    if src.get("tax_category"):
        doc.tax_category = src.tax_category

    if src.get("taxes_and_charges"):
        doc.taxes_and_charges = src.taxes_and_charges

    # ----------------------------------------------------------
    # ITEMS - with custom_remote_id matching
    # ----------------------------------------------------------
    doc.set("items", [])

    if not src.items:
        frappe.log_error(
            f"No items found in External SQTN: {src.name} - creating parent only",
            "SQ Sync - Empty Items"
        )

    # Build lookup of existing items by custom_remote_id
    existing_items_map = {}
    existing_items = []
    if existing_name:
        existing_items = frappe.db.get_all(
            "Supplier Quotation Item",
            filters={"parent": existing_name},
            fields=["name", "custom_remote_id", "item_code", "idx"],
            order_by="idx"
        )
        for ei in existing_items:
            if ei.custom_remote_id:
                existing_items_map[ei.custom_remote_id] = ei

    used_existing_items = set()
    skipped_items = []

    for idx, row in enumerate(src.items, start=1):

        resolved_item = _resolve_item_code(row.item_code, row.item_name, row.uom, company_abbr)
        if not resolved_item:
            skipped_items.append(row.item_code)
            frappe.log_error(
                f"Item '{row.item_code}' could not be resolved for SQ {src.name}. Skipping item.",
                "SQ Sync - Item Skipped"
            )
            continue

        resolved_warehouse = _resolve_warehouse(row.warehouse, src.company)

        # FIX: ERPNext rejects a Supplier Quotation Item row whose UOM isn't
        # registered on the Item (see error log: "UOM X not found in Item Y").
        # Auto-register it instead of failing the whole record.
        _ensure_item_uom(resolved_item, row.uom)

        qty  = flt(row.qty  or 0)
        rate = flt(row.rate or 0)

        # Get remote_row_id for matching
        remote_row_id = getattr(row, 'custom_remote_id', None) or row.name

        ref_fields = _build_item_reference_fields(row, company_abbr, resolved_item)

        # Check if this item already exists in the SQ
        if remote_row_id and remote_row_id in existing_items_map:
            existing_item = existing_items_map[remote_row_id]
            used_existing_items.add(existing_item.name)

            # UPDATE existing row directly in DB — DO NOT add to doc.items
            update_vals = {
                "item_code": resolved_item,
                "item_name": row.item_name,
                "description": row.get("description") or row.item_name,
                "uom": resolve_uom(row.uom),
                "qty": qty,
                "rate": rate,
                "amount": qty * rate,
                "conversion_factor": flt(getattr(row, "conversion_factor", 1) or 1),
                "base_rate": flt(getattr(row, "base_rate", rate) or rate),
                "base_amount": flt(getattr(row, "base_amount", qty * rate)),
                "warehouse": resolved_warehouse,
                "idx": idx,
                "custom_remote_id": remote_row_id,
            }
            update_vals.update(ref_fields)
            frappe.db.set_value("Supplier Quotation Item", existing_item.name, update_vals)
            continue  # <-- CRITICAL: Skip appending to doc.items

        # INSERT new row — only these go into doc.items
        item_row = {
            "item_code": resolved_item,
            "item_name": row.item_name,
            "description": row.get("description") or row.item_name,
            "uom": resolve_uom(row.uom),
            "qty": qty,
            "rate": rate,
            "amount": qty * rate,
            "conversion_factor": flt(getattr(row, "conversion_factor", 1) or 1),
            "base_rate": flt(getattr(row, "base_rate", rate) or rate),
            "base_amount": flt(getattr(row, "base_amount", qty * rate)),
            "warehouse": resolved_warehouse,
            "custom_remote_id": remote_row_id,
        }
        item_row.update(ref_fields)

        doc.append("items", item_row)

    if skipped_items:
        frappe.log_error(
            title=f"SQ {src.name} - {len(skipped_items)} items skipped",
            message="Unresolved item codes: " + ", ".join(skipped_items)
        )

    # Delete items no longer in external doc
    if existing_name:
        for ei in existing_items:
            if ei.name not in used_existing_items:
                frappe.db.delete("Supplier Quotation Item", ei.name)

    # ----------------------------------------------------------
    # TAXES
    # ----------------------------------------------------------
    doc.set("taxes", [])

    for tax in (src.get("taxes") or []):
        doc.append("taxes", {
            "charge_type": tax.get("charge_type") or "On Net Total",
            "account_head": tax.get("account_head"),
            "description": tax.get("description") or tax.get("account_head"),
            "rate": flt(tax.get("rate") or 0),
            "tax_amount": flt(tax.get("tax_amount") or 0),
            "total": flt(tax.get("total") or 0),
            "base_tax_amount": flt(tax.get("base_tax_amount") or tax.get("tax_amount") or 0),
            "base_total": flt(tax.get("base_total") or tax.get("total") or 0),
        })

    # ----------------------------------------------------------
    # FLAGS & SAVE
    # ----------------------------------------------------------
    doc.flags.ignore_permissions  = True
    doc.flags.ignore_pricing_rule = True
    doc.flags.ignore_links        = True

    if hasattr(doc, "status"):
        doc.set("status", None)

    # Calculate totals manually
    if hasattr(doc, "calculate_taxes_and_totals"):
        doc.calculate_taxes_and_totals()
    elif hasattr(doc, "set_total_taxes_and_charges"):
        doc.set_total_taxes_and_charges()

    with temporarily_enable_transaction_items(doc):
        if existing_name:
            doc.save(
                ignore_permissions=True,
                ignore_version=True,
            )
        else:
            doc.naming_series = None
            doc.insert(
                ignore_permissions=True,
                ignore_links=True,
            )

    # FIX: always (re)link External SQ -> Supplier Quotation, not just on
    # first creation — covers the case where existing_name was resolved
    # via the target_name fallback above and remote_id wasn't set yet.
    if getattr(src, "remote_id") != doc.name:
        # update_modified=False keeps modified stable so last_synced_on correctly
        # suppresses re-sync on the next scheduler run.
        src.db_set("remote_id", doc.name, update_modified=False)
    
    

    return doc

# ==========================================================
# DOCSTATUS SYNC
# ==========================================================

def sync_docstatus(doc, target_status):
    """
    Submits or cancels the document using ORM methods.
    All writes persist in the current transaction automatically.

    IMPORTANT: Both target_status and doc.docstatus are cast to int here
    because src.docstatus can arrive as a string (e.g. "1") when the value
    travels through JSON serialisation or is read from a non-submittable
    custom doctype. A string-vs-int comparison silently skips every branch,
    leaving the Supplier Quotation in Draft even when the source is Submitted.
    """
    # Always work with integers to avoid string-vs-int comparison bugs.
    target_status = int(target_status or 0)
    current = int(doc.docstatus or 0)

    if target_status == 0:
        if current in (1, 2):
            frappe.log_error(f"Cannot revert SQTN {doc.name} to Draft - already {current}", "SQ Sync")

    elif target_status == 1:
        if current == 0:
            with temporarily_enable_transaction_items(doc):
                doc.submit()  # ORM submit writes to DB

        elif current == 2:
            frappe.log_error(f"Cannot resubmit cancelled SQTN {doc.name}", "SQ Sync")

    elif target_status == 2:
        if current == 0:
            with temporarily_enable_transaction_items(doc):
                doc.submit()  # ORM submit writes to DB
                doc.cancel()  # ORM cancel writes to DB
        elif current == 1:
            with temporarily_enable_transaction_items(doc):
                doc.cancel()  # ORM cancel writes to DB
