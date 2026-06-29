import frappe
import json


@frappe.whitelist()
def sync_material_request_docs(source_doctype: str, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        external = frappe.get_doc("External Material Request", name)

        if not external.company:
            frappe.throw(f"Company missing in External Material Request: {name}")

        try:
            res = sync_external_material_request(name, external.company)
            results.append(res)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"MR Sync Failed: {name}")
            results.append({"name": name, "action": "error", "error": str(e)})

    created = [r for r in results if r["action"] == "created"]
    updated = [r for r in results if r["action"] == "updated"]
    errors  = [r for r in results if r["action"] == "error"]

    summary = f"Created: {len(created)}, Updated: {len(updated)}, Errors: {len(errors)}"

    if errors:
        frappe.msgprint(
            f"{summary}<br><br>First error ({errors[0]['name']}): {errors[0]['error']}",
            title="Material Request Sync",
            indicator="orange"
        )
    else:
        frappe.msgprint(summary, title="Material Request Sync", indicator="green")

    return {"summary": summary, "results": results}


def _build_item_rows(external, project, company):
    """Resolve and return a list of plain dicts ready for DB insert/update."""
    default_wh = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")
    rows = []
    for row in external.items:
        item_code = frappe.db.get_value("External Item", row.item_code, "remote_id")
        if not item_code:
            frappe.throw(
                f"Item mapping missing for External Item '{row.item_code}' "
                f"in External MR '{external.name}'. Sync Items first."
            )

        # Check if item is a stock item (skip non-stock items for MR)
        is_stock_item = frappe.db.get_value("Item", item_code, "is_stock_item")
        if not is_stock_item:
            frappe.log_error(
                f"Item '{item_code}' is not a stock item. Skipping in MR '{external.name}'.",
                "MR Sync - Non-Stock Item Skipped"
            )
            continue

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
            # Preserve the external row identifier for matching
            "_external_row_name": row.name,
        })
    return rows


def sync_external_material_request(external_name, company):
    external = frappe.get_doc("External Material Request", external_name)

    # 1. Determine target MR name
    target_name = external.remote_id or external.name

    # 2. Resolve project
    project = None
    if external.get("project"):
        project = frappe.db.get_value("External Project", external.project, "remote_id")

    # 3. Build resolved item rows
    item_rows = _build_item_rows(external, project, company)

    mr_exists = frappe.db.exists("Material Request", target_name)

    if mr_exists:
        # ── UPDATE path ──────────────────────────────────────────────────────

        # 3a. Update ALL header fields directly in DB (bypass controller validation)
        update_fields = {
            "material_request_type": external.material_request_type,
            "company":               company,
            "transaction_date":      external.transaction_date,
            "schedule_date":         external.schedule_date,
            "status":                external.status or "Draft",
        }

        # Optional fields — only set if they exist on the external doc
        optional_fields = [
            "buying_price_list",
            "letter_head",
            "naming_series",
            "set_warehouse",
            "title",
            "transfer_status",
            "amended_from",
            "source_site",
            "remote_id",
        ]
        for field in optional_fields:
            value = external.get(field)
            if value is not None:
                update_fields[field] = value

        frappe.db.set_value(
            "Material Request",
            target_name,
            update_fields,
            update_modified=True,
        )

        # 3b. PRESERVE existing child rows - match by EXTERNAL ROW NAME (not item_code!)
        # This is the KEY FIX: use _external_row_name to track row identity
        existing_rows = frappe.db.get_all(
            "Material Request Item",
            filters={"parent": target_name},
            fields=["name", "item_code", "idx", "custom_external_row_name"],  # Add custom field
            order_by="idx"
        )

        # Build lookup: external_row_name -> existing row name
        # First, try matching by custom_external_row_name if you have it
        # Fallback: match by item_code + idx combination for backward compatibility
        existing_by_external = {}
        existing_by_item_idx = {}
        for r in existing_rows:
            if r.get("custom_external_row_name"):
                existing_by_external[r.custom_external_row_name] = r
            # Also index by (item_code, idx) as fallback
            existing_by_item_idx[(r.item_code, r.idx)] = r

        # Track which existing rows are still used
        used_existing_names = set()

        for idx, item_data in enumerate(item_rows, start=1):
            external_row_name = item_data["_external_row_name"]
            item_code = item_data["item_code"]

            # PRIORITY 1: Match by external row name (most reliable)
            if external_row_name in existing_by_external:
                existing = existing_by_external[external_row_name]
                used_existing_names.add(existing.name)
                
                frappe.db.set_value("Material Request Item", existing.name, {
                    "item_code":     item_data["item_code"],
                    "item_name":     item_data["item_name"],
                    "description":   item_data["description"],
                    "qty":           item_data["qty"],
                    "uom":           item_data["uom"],
                    "schedule_date": item_data["schedule_date"],
                    "warehouse":     item_data["warehouse"],
                    "project":       item_data["project"],
                    "idx":           idx,
                    "custom_external_row_name": external_row_name,  # Store for future matching
                })

            # PRIORITY 2: Fallback - match by (item_code, original_idx) from external
            # This handles legacy rows without custom_external_row_name
            elif (item_code, idx) in existing_by_item_idx:
                existing = existing_by_item_idx[(item_code, idx)]
                used_existing_names.add(existing.name)
                
                frappe.db.set_value("Material Request Item", existing.name, {
                    "item_code":     item_data["item_code"],
                    "item_name":     item_data["item_name"],
                    "description":   item_data["description"],
                    "qty":           item_data["qty"],
                    "uom":           item_data["uom"],
                    "schedule_date": item_data["schedule_date"],
                    "warehouse":     item_data["warehouse"],
                    "project":       item_data["project"],
                    "idx":           idx,
                    "custom_external_row_name": external_row_name,
                })

            else:
                # INSERT new row
                child_name = frappe.generate_hash("", 10)
                frappe.db.sql(
                    """INSERT INTO `tabMaterial Request Item`
                       (name, parent, parenttype, parentfield, idx,
                        item_code, item_name, description,
                        qty, uom, schedule_date, warehouse, project,
                        custom_external_row_name)
                       VALUES (%(name)s, %(parent)s, 'Material Request', 'items', %(idx)s,
                               %(item_code)s, %(item_name)s, %(description)s,
                               %(qty)s, %(uom)s, %(schedule_date)s, %(warehouse)s, %(project)s,
                               %(external_row_name)s)
                    """,
                    {
                        "name":   child_name,
                        "parent": target_name,
                        "idx":    idx,
                        "item_code":     item_data["item_code"],
                        "item_name":     item_data["item_name"],
                        "description":   item_data["description"],
                        "qty":           item_data["qty"],
                        "uom":           item_data["uom"],
                        "schedule_date": item_data["schedule_date"],
                        "warehouse":     item_data["warehouse"],
                        "project":       item_data["project"],
                        "external_row_name": external_row_name,
                    },
                )

        # 3c. DELETE only rows that are no longer in the external doc
        rows_to_delete = [r.name for r in existing_rows if r.name not in used_existing_names]

        # Safety check: don't delete rows that are referenced by other docs
        for row_name in rows_to_delete:
            # Check if this row is referenced by Stock Entries or other docs
            refs = frappe.db.sql("""
                SELECT parenttype, parent FROM `tabStock Entry Detail`
                WHERE material_request_item = %s
                UNION ALL
                SELECT parenttype, parent FROM `tabPurchase Order Item`
                WHERE material_request_item = %s
            """, (row_name, row_name))

            if refs:
                # Row is referenced - set qty to 0 instead of deleting
                frappe.db.set_value("Material Request Item", row_name, {
                    "qty": 0,
                    "idx": 999,  # Move to end
                })
                frappe.log_error(
                    f"MR Item row {row_name} referenced by {refs}. Set qty=0 instead of deleting.",
                    "MR Sync - Referenced Row Preserved"
                )
            else:
                frappe.db.delete("Material Request Item", row_name)

        # 3d. Sync docstatus if needed
        remote_status = frappe.utils.cint(external.docstatus)
        if remote_status > 0:
            frappe.db.set_value(
                "Material Request", target_name, "docstatus", remote_status, update_modified=False
            )

        return {"name": target_name, "action": "updated"}

    else:
        # ── CREATE path ──────────────────────────────────────────────────────
        mr = frappe.new_doc("Material Request")
        mr.name                  = target_name
        mr.material_request_type = external.material_request_type
        mr.company               = company
        mr.transaction_date      = external.transaction_date
        mr.schedule_date         = external.schedule_date
        mr.status                = external.status or "Draft"

        # Optional fields — copy if present on the external doc
        for field in [
            "buying_price_list", "letter_head", "naming_series",
            "set_warehouse", "title", "transfer_status", "amended_from",
            "source_site", "remote_id",
        ]:
            value = external.get(field)
            if value is not None:
                mr.set(field, value)

        for item_data in item_rows:
            mr.append("items", {
                "item_code":     item_data["item_code"],
                "item_name":     item_data["item_name"],
                "description":   item_data["description"],
                "qty":           item_data["qty"],
                "uom":           item_data["uom"],
                "schedule_date": item_data["schedule_date"],
                "warehouse":     item_data["warehouse"],
                "project":       item_data["project"],
                "custom_external_row_name": item_data["_external_row_name"],  # Store external row name
            })

        mr.flags.ignore_links     = True
        mr.flags.ignore_mandatory = True
        mr.db_insert()

        for item in mr.items:
            item.parent      = mr.name
            item.parenttype  = "Material Request"
            item.parentfield = "items"
            if not item.name:
                item.name = frappe.generate_hash("", 10)
            item.flags.ignore_links     = True
            item.flags.ignore_mandatory = True
            item.db_insert()

        remote_status = frappe.utils.cint(external.docstatus)
        if remote_status > 0:
            frappe.db.set_value(
                "Material Request", mr.name, "docstatus", remote_status, update_modified=False
            )

        external.db_set("remote_id", mr.name)
        return {"name": mr.name, "action": "created"}