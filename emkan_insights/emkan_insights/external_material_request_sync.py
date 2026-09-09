import frappe
import json
from frappe.utils import cint
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import (
    sync_record_guard,
    temporarily_enable_transaction_items,
)
from emkan_insights.emkan_insights.uom_resolver import resolve_uom

VALID_MATERIAL_REQUEST_STATUSES = {
    "Draft",
    "Submitted",
    "Stopped",
    "Cancelled",
    "Pending",
    "Partially Ordered",
    "Ordered",
    "Issued",
    "Transferred",
    "Received",
}


TARGET_SYNC_LOCK_TTL = 7200
TARGET_BATCH_SIZE = 500


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


def _enqueue_material_request_target_batch(source_doctype, names, batch_index=0):
    if batch_index * TARGET_BATCH_SIZE >= len(names):
        return

    frappe.enqueue(
        "emkan_insights.emkan_insights.external_material_request_sync.sync_bulk_material_requests",
        queue="long",
        names=names,
        source_doctype=source_doctype,
        batch_index=batch_index,
        timeout=2000,
        job_name=f"mr_sync_batch_{batch_index + 1}",
        enqueue_after_commit=True,
    )


def _get_safe_material_request_status(external):
    status = (external.get("status") or "").strip()
    if status in VALID_MATERIAL_REQUEST_STATUSES:
        return status

    docstatus = cint(external.get("docstatus"))
    if docstatus == 1:
        return "Submitted"
    if docstatus == 2:
        return "Cancelled"
    return "Draft"


@frappe.whitelist()
@filter_kwargs
def sync_material_request_docs(source_doctype: str, names, sync_now=False):
    if isinstance(names, str):
        names = json.loads(names)

    if not names:
        names = frappe.db.sql(f"""
            SELECT name
            FROM `tab{source_doctype}`
            WHERE
                last_synced_on IS NULL
                OR modified >= last_synced_on
        """, pluck=True)

    names = list(dict.fromkeys(names))
    if not names:
        return {"message": "No External Material Request records pending sync", "queued": 0, "batches": 0}

    if sync_now:
        # Sequential pipeline path: strict fail-fast with sequential chunks.
        # The target lock is held for the whole run and released on every exit.
        if not _acquire_target_sync_lock(source_doctype):
            frappe.throw(f"{source_doctype} target sync is already running")
        try:
            return run_chunks_fail_fast(
                names,
                TARGET_BATCH_SIZE,
                _process_material_request_chunk,
            )
        finally:
            _release_target_sync_lock(source_doctype)

    if not _acquire_target_sync_lock(source_doctype):
        return {
            "message": f"{source_doctype} target sync is already running",
            "queued": 0,
            "batches": 0
        }

    _enqueue_material_request_target_batch(source_doctype, names, batch_index=0)

    return {
        "message": f"Sync Queued: {len(names)} records in {(len(names) + TARGET_BATCH_SIZE - 1) // TARGET_BATCH_SIZE} sequential batches",
        "queued": len(names),
        "batches": (len(names) + TARGET_BATCH_SIZE - 1) // TARGET_BATCH_SIZE
    }


def _process_material_request_chunk(chunk):
    """Strict fail-fast record loop for one chunk of External Material Requests."""
    results = []

    for name in chunk:
        savepoint = f"mr_sync_{frappe.generate_hash(length=10)}"
        try:
            with sync_record_guard("External Material Request", name):
                frappe.db.savepoint(savepoint)

                external = frappe.get_doc("External Material Request", name)

                if not external.company:
                    raise Exception(f"Company missing in External Material Request: {name}")

                res = sync_external_material_request(name, external.company)
                results.append(res)

        except Exception as e:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
            log_record_failure("Material Request", name, str(e))
            raise

    return results


def sync_bulk_material_requests(names, source_doctype="External Material Request", batch_index=0):
    """
    Processed as chained background batches so only one batch writes target
    Material Requests at a time. Strictly fail-fast: the first record failure
    aborts the chain and releases the target lock.
    """
    if isinstance(names, str):
        names = json.loads(names)

    batch_index = int(batch_index or 0)
    chunk = names[batch_index * TARGET_BATCH_SIZE:(batch_index + 1) * TARGET_BATCH_SIZE]

    try:
        results = _process_material_request_chunk(chunk)
    except Exception:
        # The chain aborted: release the target lock so a failed run does
        # not block subsequent runs until TTL expiry.
        _release_target_sync_lock(source_doctype)
        raise

    created = [r for r in results if r["action"] == "created"]
    updated = [r for r in results if r["action"] == "updated"]
    errors  = [r for r in results if r["action"] == "error"]
    skipped_lines = [s for r in results for s in r.get("skipped", [])]

    summary = f"Created: {len(created)}, Updated: {len(updated)}, Errors: {len(errors)}"
    if skipped_lines:
        summary += f", Skipped item rows: {len(skipped_lines)}"

    frappe.log_error(
        title="Material Request Sync Batch Complete",
        message=f"Summary: {summary}\nDetails: {json.dumps(results, default=str, indent=2)}"
    )

    next_batch_index = batch_index + 1
    if next_batch_index * TARGET_BATCH_SIZE < len(names):
        _enqueue_material_request_target_batch(source_doctype, names, next_batch_index)
    else:
        frappe.db.after_commit.add(
            lambda: _release_target_sync_lock(source_doctype)
        )

    return {"summary": summary, "results": results}


def _resolve_item_code(row_item_code, external_mr_name):
    variants = [row_item_code]
    if not row_item_code.startswith("0"):
        variants.append("0" + row_item_code)
    if row_item_code.startswith("0") and len(row_item_code) > 1:
        variants.append(row_item_code.lstrip("0"))

    for variant in variants:
        ext_item = frappe.db.get_value("External Item", variant,
            ["name", "remote_id", "item_code", "item_name", "item_group", "stock_uom", "description"], as_dict=1)
        if ext_item:
            if ext_item.remote_id and frappe.db.exists("Item", ext_item.remote_id):
                return ext_item.remote_id, None
            if ext_item.remote_id:
                return _auto_create_item(ext_item), None
            return _auto_create_item(ext_item), None

    for variant in variants:
        ext_item = frappe.db.get_value("External Item", {"item_code": variant},
            ["name", "remote_id", "item_code", "item_name", "item_group", "stock_uom", "description"], as_dict=1)
        if ext_item:
            if ext_item.remote_id and frappe.db.exists("Item", ext_item.remote_id):
                return ext_item.remote_id, None
            if ext_item.remote_id:
                return _auto_create_item(ext_item), None
            return _auto_create_item(ext_item), None

    # Fallback: the target local Item may already exist directly (created by a
    # prior Item sync or manually) even when there is no External Item mapping
    # row. Use it so valid MR lines are not silently dropped.
    for variant in variants:
        if frappe.db.exists("Item", variant):
            return variant, None
        local_by_code = frappe.db.get_value("Item", {"item_code": variant}, "name")
        if local_by_code:
            return local_by_code, None

    return None, f"External Item '{row_item_code}' not found in External Item table"


def _auto_create_item(ext_item):
    item_code = ext_item.item_code or ext_item.name

    if frappe.db.exists("Item", item_code):
        frappe.db.set_value("External Item", ext_item.name, "remote_id", item_code)
        return item_code

    new_item = frappe.new_doc("Item")
    new_item.item_code = item_code
    new_item.item_name = ext_item.item_name or item_code
    company_abbr = frappe.db.get_value("Company", getattr(ext_item, "company", None), "abbr")
    item_group = ext_item.item_group or "All Item Groups"
    prefixed_item_group = f"{company_abbr}-{item_group}" if company_abbr else item_group
    new_item.item_group = prefixed_item_group if frappe.db.exists("Item Group", prefixed_item_group) else item_group
    new_item.stock_uom = ext_item.stock_uom or "Nos"
    new_item.is_stock_item = 1
    new_item.description = ext_item.description or ""

    try:
        new_item.insert()
        frappe.db.set_value("External Item", ext_item.name, "remote_id", new_item.name)
        frappe.log_error(f"Auto-created Item {new_item.name} from External Item {ext_item.name}", "MR Sync")
        return new_item.name
    except Exception as e:
        frappe.log_error(f"Failed to auto-create item {item_code}: {str(e)}", "MR Sync")
        return None


def _build_item_rows(external, project, company):
    default_wh = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")
    rows = []
    skipped = []
    seen_remote_row_ids = set()

    for row in external.items:
        item_code, error_reason = _resolve_item_code(row.item_code, external.name)

        if not item_code:
            frappe.log_error(
                f"{error_reason} in External MR '{external.name}'",
                "MR Sync - Item Skipped"
            )
            skipped.append({
                "mr_name": external.name,
                "item_code": row.item_code,
                "reason": error_reason
            })
            continue

        remote_row_id = row.custom_remote_id or row.name
        if not remote_row_id:
            reason = "External row has no identifier"
            skipped.append({"mr_name": external.name, "item_code": item_code, "reason": reason})
            continue
        if remote_row_id in seen_remote_row_ids:
            continue
        seen_remote_row_ids.add(remote_row_id)

        warehouse = frappe.db.get_value("External Warehouse", row.warehouse, "remote_id") or default_wh

        rows.append({
            "item_code":     item_code,
            "item_name":     row.item_name,
            "description":   row.description,
            "qty":           row.qty,
            "uom":           resolve_uom(row.uom),
            "schedule_date": row.schedule_date,
            "warehouse":     warehouse,
            "project":       project,
            "remote_row_id": remote_row_id,
        })

    return rows, skipped


def _get_company_abbr(company):
    if not company:
        return "EXT"
    return frappe.db.get_value("Company", company, "abbr") or "EXT"


# def _build_local_mr_name(company, external_name):
#     """Build the canonical local MR name: {company_abbr}-{remote_name}."""
#     abbr = _get_company_abbr(company)
#     external_name = (external_name or "").strip()
#     if external_name.startswith(f"{abbr}-"):
#         return external_name
#     return f"{abbr}-{external_name}"

def _build_local_mr_name(company, external_name):
    """Build the canonical local MR name: {company_abbr}-{remote_name}."""
    abbr = _get_company_abbr(company)
    external_name = (external_name or "").strip()

    # Always prefix for Incharge Company
    if company == "Incharge Company":
        return f"{abbr}-{external_name}"

    # Avoid double prefix for other companies
    if external_name.startswith(f"{abbr}-"):
        return external_name

    return f"{abbr}-{external_name}"


def _resolve_local_mr_target(external, company):
    
    expected_name = _build_local_mr_name(company, external.name)

    # 1. Match by local remote_id + company (authoritative link)
    target_name = frappe.db.get_value(
        "Material Request",
        {"remote_id": external.name, "company": company},
        "name",
    )
    if target_name:
        return target_name

    # 2. Canonical company-prefixed name for this external doc
    if frappe.db.exists("Material Request", expected_name):
        if frappe.db.get_value("Material Request", expected_name, "company") == company:
            return expected_name

    # 3. external.remote_id may be unprefixed (from fetch) or already prefixed
    #    (from a previous sync). Try both, always with company scope.
    if external.remote_id:
        remote_id_candidates = [external.remote_id]
        prefixed_remote_id = _build_local_mr_name(company, external.remote_id)
        if prefixed_remote_id not in remote_id_candidates:
            remote_id_candidates.append(prefixed_remote_id)

        for candidate in remote_id_candidates:
            if not frappe.db.exists("Material Request", candidate):
                continue
            if frappe.db.get_value("Material Request", candidate, "company") == company:
                return candidate

        if not any(frappe.db.exists("Material Request", c) for c in remote_id_candidates):
            frappe.db.set_value(
                "External Material Request", external.name, "remote_id", None
            )

    return None


def _resolve_local_amendment_parent(remote_amended_from, company):
    if not remote_amended_from:
        return None

    remote_amended_from = str(remote_amended_from).strip()
    if not remote_amended_from:
        return None

    parent = frappe._dict(name=remote_amended_from, remote_id=None)
    local_name = _resolve_local_mr_target(parent, company)
    if not local_name:
        frappe.throw(
            f"Cannot sync amendment because its local parent was not found: {remote_amended_from}",
            title="Amendment Parent Missing",
        )

    if cint(frappe.db.get_value("Material Request", local_name, "docstatus")) != 2:
        frappe.throw(
            f"{local_name} must be cancelled before its amendment can be synced.",
            title="Amendment Not Allowed",
        )

    return local_name


def _resolve_optional_link(doctype, value):
    if not value:
        return None

    value = str(value).strip()
    if value and frappe.db.exists(doctype, value):
        return value

    return None


def sync_external_material_request(external_name, company):
    external = frappe.get_doc("External Material Request", external_name)
    sync_started_at = external.modified
    safe_status = _get_safe_material_request_status(external)

    target_name = _resolve_local_mr_target(external, company)
    expected_name = _build_local_mr_name(company, external.name)
    amended_from = _resolve_local_amendment_parent(external.get("amended_from"), company)

    project = None
    if external.get("project"):
        project = frappe.db.get_value("External Project", external.project, "remote_id")

    item_rows, skipped = _build_item_rows(external, project, company)
    letter_head = _resolve_optional_link("Letter Head", external.get("letter_head"))
    if external.get("letter_head") and not letter_head:
        skipped.append({
            "mr_name": external.name,
            "field": "letter_head",
            "reason": f"Letter Head '{external.get('letter_head')}' does not exist locally",
        })

    if target_name:
        header_fields = {
            "material_request_type": external.material_request_type,
            "company": company,
            "transaction_date": external.transaction_date,
            "schedule_date": external.schedule_date,
            "status": safe_status,
            "remote_id": external.name,
        }
        for field in ["buying_price_list", "set_warehouse",
                      "title", "transfer_status", "source_site"]:
            value = external.get(field)
            if value is not None:
                header_fields[field] = value
        if amended_from:
            header_fields["amended_from"] = amended_from
        if letter_head:
            header_fields["letter_head"] = letter_head

        frappe.db.set_value("Material Request", target_name, header_fields, update_modified=True)

        # Only update child rows when the MR is in draft (docstatus=0).
        # For already-submitted MRs, Frappe's validate_update_after_submit blocks
        # changes to fields like UOM, raising UpdateAfterSubmitError. The item
        # rows are already correct from the initial sync; skip the update.
        existing_docstatus = frappe.db.get_value("Material Request", target_name, "docstatus")
        if cint(existing_docstatus) == 0:
            existing_rows = frappe.db.get_all(
                "Material Request Item",
                filters={"parent": target_name, "parenttype": "Material Request"},
                fields=["name", "custom_remote_id", "item_code", "warehouse", "idx"],
                order_by="idx"
            )

            existing_by_remote_id = {}
            existing_by_item_wh = {}

            for r in existing_rows:
                if r.get("custom_remote_id"):
                    existing_by_remote_id[r.custom_remote_id] = r
                key = f"{r.item_code}|{r.warehouse or ''}"
                existing_by_item_wh[key] = r

            used_existing = set()
            new_rows = []

            for idx, item_data in enumerate(item_rows, start=1):
                remote_row_id = item_data.get("remote_row_id")
                matched = None

                if remote_row_id and remote_row_id in existing_by_remote_id:
                    matched = existing_by_remote_id[remote_row_id]

                if not matched:
                    fallback_key = f"{item_data['item_code']}|{item_data['warehouse'] or ''}"
                    if fallback_key in existing_by_item_wh:
                        matched = existing_by_item_wh[fallback_key]

                if matched:
                    used_existing.add(matched.name)

                    frappe.db.set_value("Material Request Item", matched.name, {
                        "item_code": item_data["item_code"],
                        "item_name": item_data["item_name"],
                        "description": item_data["description"],
                        "qty": item_data["qty"],
                        "uom": item_data["uom"],
                        "schedule_date": item_data["schedule_date"],
                        "warehouse": item_data["warehouse"],
                        "project": item_data["project"],
                        "idx": idx,
                        "custom_remote_id": remote_row_id,
                    })
                else:
                    new_rows.append({"idx": idx, "data": item_data})

            for new_row in new_rows:
                idx = new_row["idx"]
                item_data = new_row["data"]
                child_name = frappe.generate_hash("", 10)

                frappe.db.sql("""
                    INSERT INTO `tabMaterial Request Item`
                    (name, parent, parenttype, parentfield, idx,
                     item_code, item_name, description,
                     qty, uom, schedule_date, warehouse, project,
                     custom_remote_id)
                    VALUES (%(name)s, %(parent)s, 'Material Request', 'items', %(idx)s,
                            %(item_code)s, %(item_name)s, %(description)s,
                            %(qty)s, %(uom)s, %(schedule_date)s, %(warehouse)s, %(project)s,
                            %(remote_row_id)s)
                """, {
                    "name": child_name,
                    "parent": target_name,
                    "idx": idx,
                    "item_code": item_data["item_code"],
                    "item_name": item_data["item_name"],
                    "description": item_data["description"],
                    "qty": item_data["qty"],
                    "uom": item_data["uom"],
                    "schedule_date": item_data["schedule_date"],
                    "warehouse": item_data["warehouse"],
                    "project": item_data["project"],
                    "remote_row_id": item_data["remote_row_id"] or "",
                })

            for r in existing_rows:
                if r.name not in used_existing:
                    refs = frappe.db.sql("""
                        SELECT parenttype, parent FROM `tabStock Entry Detail`
                        WHERE material_request_item = %s
                        UNION ALL
                        SELECT parenttype, parent FROM `tabPurchase Order Item`
                        WHERE material_request_item = %s
                        LIMIT 1
                    """, (r.name, r.name))

                    if refs:
                        frappe.db.set_value("Material Request Item", r.name, {
                            "qty": 0,
                            "idx": 999,
                        })
                    else:
                        frappe.db.delete("Material Request Item", r.name)

        remote_status = cint(external.docstatus)
        local_docstatus = cint(existing_docstatus)
        if remote_status == 1 and local_docstatus == 0:
            # Only submit if the local MR is still in draft.
            try:
                mr_doc = frappe.get_doc("Material Request", target_name)
                mr_doc.flags.ignore_permissions = True
                with temporarily_enable_transaction_items(mr_doc):
                    mr_doc.submit()  # ORM submit — runs all lifecycle hooks
            except Exception as submit_err:
                frappe.log_error(
                    title=f"CRITICAL: MR {target_name} ORM submit failed — manual intervention required",
                    message=(
                        f"Error: {str(submit_err)}\n\n{frappe.get_traceback()}\n\n"
                        f"The Material Request has NOT been submitted. "
                        f"Investigate why submit() failed and submit manually via ERPNext."
                    )
                )
                raise
        elif remote_status == 2:
            try:
                mr_doc = frappe.get_doc("Material Request", target_name)
                mr_doc.flags.ignore_permissions = True
                with temporarily_enable_transaction_items(mr_doc):
                    if local_docstatus == 0:
                        # Draft → must submit first, then cancel.
                        mr_doc.submit()
                        mr_doc.reload()
                    if local_docstatus in (0, 1):
                        # Already submitted (or just submitted above) → cancel.
                        mr_doc.cancel()
                    # local_docstatus == 2: already cancelled, nothing to do.
            except Exception as lifecycle_err:
                frappe.log_error(
                    title=f"CRITICAL: MR {target_name} ORM submit/cancel failed — manual intervention required",
                    message=(
                        f"Error: {str(lifecycle_err)}\n\n{frappe.get_traceback()}\n\n"
                        f"The Material Request lifecycle could not complete. "
                        f"Investigate and correct the document manually via ERPNext."
                    )
                )
                raise

        frappe.db.set_value(
            "External Material Request", external.name,
            "remote_id", target_name,
            update_modified=False,
        )
        external.db_set(
            "last_synced_on",
            sync_started_at,
            update_modified=False
        )

        return {"name": target_name, "action": "updated", "skipped": skipped}

    else:
        mr = frappe.new_doc("Material Request")
        mr.name = target_name = expected_name
        mr.flags.name_set = True

        mr.material_request_type = external.material_request_type
        mr.company = company
        mr.transaction_date = external.transaction_date
        mr.schedule_date = external.schedule_date
        mr.status = safe_status
        mr.remote_id = external.name

        for field in ["buying_price_list", "set_warehouse",
                      "title", "transfer_status", "source_site"]:
            value = external.get(field)
            if value is not None:
                mr.set(field, value)
        if amended_from:
            mr.amended_from = amended_from
        if letter_head:
            mr.letter_head = letter_head

        for item_data in item_rows:
            mr.append("items", {
                "item_code": item_data["item_code"],
                "item_name": item_data["item_name"],
                "description": item_data["description"],
                "qty": item_data["qty"],
                "uom": item_data["uom"],
                "schedule_date": item_data["schedule_date"],
                "warehouse": item_data["warehouse"],
                "project": item_data["project"],
                "custom_remote_id": item_data["remote_row_id"],
            })

        # ORM insert: runs validate/before_insert/after_insert hooks and
        # cascades the already-appended child rows automatically.
        # ignore_mandatory=True tolerates missing non-critical fields
        # (e.g. items table validated after insert).
        # flags.name_set=True (set above) preserves the custom name.
        with temporarily_enable_transaction_items(mr):
            mr.insert(ignore_permissions=True, ignore_mandatory=True)

        remote_status = cint(external.docstatus)
        if remote_status == 1:
            try:
                mr_doc = frappe.get_doc("Material Request", mr.name)
                mr_doc.flags.ignore_permissions = True
                with temporarily_enable_transaction_items(mr_doc):
                    mr_doc.submit()  # ORM submit — runs all lifecycle hooks
            except Exception as submit_err:
                frappe.log_error(
                    title=f"CRITICAL: MR {mr.name} ORM submit failed — manual intervention required",
                    message=(
                        f"Error: {str(submit_err)}\n\n{frappe.get_traceback()}\n\n"
                        f"The Material Request has NOT been submitted. "
                        f"Investigate why submit() failed and submit manually via ERPNext."
                    )
                )
                raise
        elif remote_status == 2:
            try:
                mr_doc = frappe.get_doc("Material Request", mr.name)
                mr_doc.flags.ignore_permissions = True
                with temporarily_enable_transaction_items(mr_doc):
                    mr_doc.submit()  # Must submit first before cancel
                    mr_doc.reload()
                    mr_doc.cancel()  # ORM cancel — runs all lifecycle hooks
            except Exception as lifecycle_err:
                frappe.log_error(
                    title=f"CRITICAL: MR {mr.name} ORM submit/cancel failed — manual intervention required",
                    message=(
                        f"Error: {str(lifecycle_err)}\n\n{frappe.get_traceback()}\n\n"
                        f"The Material Request lifecycle could not complete. "
                        f"Investigate and correct the document manually via ERPNext."
                    )
                )
                raise

        frappe.db.set_value(
            "External Material Request", external.name,
            "remote_id", mr.name,
            update_modified=False,
        )
        external.db_set(
            "last_synced_on",
            sync_started_at,
            update_modified=False
        )

        return {"name": mr.name, "action": "created", "skipped": skipped}
