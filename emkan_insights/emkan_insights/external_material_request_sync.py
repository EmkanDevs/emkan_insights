import frappe
import json


@frappe.whitelist()
def sync_material_request_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    names = list(dict.fromkeys(names))
    chunk_size = 500

    for i in range(0, len(names), chunk_size):
        chunk = names[i:i + chunk_size]
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_material_request_sync.sync_bulk_material_requests",
            queue="long",
            names=chunk,
            timeout=2000,
            job_name=f"mr_sync_batch_{i // chunk_size + 1}"
        )

    return {
        "message": f"Sync Queued: {len(names)} records in {(len(names) + chunk_size - 1) // chunk_size} batches",
        "queued": len(names),
        "batches": (len(names) + chunk_size - 1) // chunk_size
    }


def sync_bulk_material_requests(names):
    """
    Processed in background queue.
    All database writes happen within a single transaction.
    Frappe automatically commits the transaction when the background job finishes.
    """
    results = []

    for name in names:
        try:
            external = frappe.get_doc("External Material Request", name)

            if not external.company:
                results.append({
                    "name": name,
                    "action": "error",
                    "error": f"Company missing in External Material Request: {name}"
                })
                continue

            res = sync_external_material_request(name, external.company)
            results.append(res)

        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"MR Sync Failed: {name}")
            results.append({
                "name": name,
                "action": "error",
                "error": str(e)
            })

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
    new_item.item_group = ext_item.item_group or "All Item Groups"
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
            "uom":           row.uom,
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


def sync_external_material_request(external_name, company):
    external = frappe.get_doc("External Material Request", external_name)

    target_name = _resolve_local_mr_target(external, company)
    expected_name = _build_local_mr_name(company, external.name)

    project = None
    if external.get("project"):
        project = frappe.db.get_value("External Project", external.project, "remote_id")

    item_rows, skipped = _build_item_rows(external, project, company)

    if target_name:
        header_fields = {
            "material_request_type": external.material_request_type,
            "company": company,
            "transaction_date": external.transaction_date,
            "schedule_date": external.schedule_date,
            "status": external.status or "Draft",
            "remote_id": external.name,
        }
        for field in ["buying_price_list", "letter_head", "set_warehouse",
                      "title", "transfer_status", "amended_from", "source_site"]:
            value = external.get(field)
            if value is not None:
                header_fields[field] = value

        frappe.db.set_value("Material Request", target_name, header_fields, update_modified=True)

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

        remote_status = frappe.utils.cint(external.docstatus)
        if remote_status > 0:
            frappe.db.set_value(
                "Material Request", target_name,
                "docstatus", remote_status, update_modified=False
            )

        frappe.db.set_value(
            "External Material Request", external.name,
            "remote_id", target_name
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
        mr.status = external.status or "Draft"
        mr.remote_id = external.name

        for field in ["buying_price_list", "letter_head", "set_warehouse",
                      "title", "transfer_status", "amended_from", "source_site"]:
            value = external.get(field)
            if value is not None:
                mr.set(field, value)

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

        mr.db_insert()

        for item in mr.items:
            item.parent = mr.name
            item.parenttype = "Material Request"
            item.parentfield = "items"
            if not item.name:
                item.name = frappe.generate_hash("", 10)
            item.db_insert()

        remote_status = frappe.utils.cint(external.docstatus)
        if remote_status > 0:
            frappe.db.set_value(
                "Material Request", mr.name,
                "docstatus", remote_status, update_modified=False
            )

        frappe.db.set_value(
            "External Material Request", external.name,
            "remote_id", mr.name
        )

        return {"name": mr.name, "action": "created", "skipped": skipped}