import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}


def _log(title, message):
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    if not remote_or_local_name:
        return None

    if company_abbr and not remote_or_local_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_name}"
        if frappe.db.exists(doctype, prefixed):
            return prefixed

    for remote_field in ("custom_remote_id", "remote_id"):
        if frappe.get_meta(doctype).has_field(remote_field):
            local_name = frappe.db.get_value(doctype, {remote_field: remote_or_local_name}, "name")
            if local_name:
                return local_name

    if frappe.db.exists(doctype, remote_or_local_name):
        return remote_or_local_name

    return None


def get_child_detail(doctype, parent, item_code, remote_item_name=None):
    if not parent:
        return None

    if remote_item_name and frappe.get_meta(doctype).has_field("custom_remote_id"):
        exact = frappe.db.get_value(
            doctype,
            {"parent": parent, "custom_remote_id": remote_item_name},
            "name"
        )
        if exact:
            return exact

    return frappe.db.get_value(
        doctype,
        {"parent": parent, "item_code": item_code},
        "name"
    )


@frappe.whitelist()
def sync_rfq_docs(source_doctype: str, names):
    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            frappe.db.commit()

            external = frappe.get_doc("External Request for Quotation", name)

            if not external.company:
                frappe.throw(f"Company missing in External RFQ: {name}")

            # -------------------------------------------------
            # BUILD TARGET NAME
            # -------------------------------------------------
            company_abbr = frappe.db.get_value("Company", external.company, "abbr") or ""
            remote_id = external.name

            if company_abbr and remote_id.startswith(f"{company_abbr}-"):
                target_name = remote_id
            else:
                target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # -------------------------------------------------
            # CHECK ALREADY SYNCED
            # -------------------------------------------------
            existing = frappe.db.get_value(
                "Request for Quotation", {"custom_remote_id": remote_id}, "name"
            )
            if not existing:
                existing = frappe.db.exists("Request for Quotation", target_name)

            if existing:
                results.append({"name": existing, "status": "exists"})
                continue

            # -------------------------------------------------
            # CREATE RFQ
            # -------------------------------------------------
            rfq = frappe.new_doc("Request for Quotation")

            rfq.name = target_name
            rfq.flags.name_set = True

            # CRITICAL: naming_series must be set or submit fails
            rfq.naming_series = external.naming_series or "PUR-RFQ-.YYYY.-"

            rfq.company = external.company
            rfq.transaction_date = external.transaction_date
            rfq.message_for_supplier = external.message_for_supplier or "Request for Quotation"

            # CRITICAL: status must be "Draft" for insert, then submit changes it
            rfq.status = "Draft"

            # Copy all other header fields
            for field, value in external.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in {"items", "suppliers", "remote_id", "custom_remote_id",
                                      "naming_series", "company", "transaction_date",
                                      "message_for_supplier", "status", "docstatus",
                                      "amended_from", "auto_repeat"}
                    and hasattr(rfq, field)
                ):
                    rfq.set(field, value)

            # CRITICAL: Clear amendment fields that block submit
            rfq.amended_from = None
            if hasattr(rfq, "auto_repeat"):
                rfq.auto_repeat = None

            rfq.custom_remote_id = remote_id
            rfq.custom_source_site = external.source_site

            # Resolve project
            if external.get("project"):
                rfq.project = resolve_local_doc("External Project", external.project, company_abbr)

            # -------------------------------------------------
            # MAP SUPPLIERS
            # -------------------------------------------------
            seen_suppliers = set()
            for s in external.get("suppliers", []):
                supplier_code = frappe.db.get_value("External Supplier", s.supplier, "remote_id")
                if not supplier_code:
                    continue

                fingerprint = (supplier_code, s.contact)
                if fingerprint in seen_suppliers:
                    continue
                seen_suppliers.add(fingerprint)

                rfq.append("suppliers", {
                    "supplier": supplier_code,
                    "contact": s.contact,
                    "quote_status": s.quote_status or "Pending",
                    "supplier_name": s.supplier_name,
                    "email_id": s.email_id,
                    "send_email": s.send_email or 0,
                    "email_sent": s.email_sent or 0,
                })

            # -------------------------------------------------
            # MAP ITEMS
            # -------------------------------------------------
            seen_items = set()
            for row in external.get("items", []):
                item_code = frappe.db.get_value("External Item", row.item_code, "remote_id")
                if not item_code:
                    _log(f"RFQ item skip - no local item for {row.item_code}",
                         f"External RFQ {name}, row {row.name}")
                    continue

                # Resolve warehouse
                warehouse = None
                if row.warehouse:
                    warehouse = frappe.db.get_value("External Warehouse", row.warehouse, "remote_id")
                if not warehouse:
                    warehouse = frappe.db.get_value(
                        "Warehouse", {"is_group": 0, "company": external.company}, "name"
                    )

                fingerprint = (item_code, row.qty, row.schedule_date, warehouse)
                if fingerprint in seen_items:
                    continue
                seen_items.add(fingerprint)

                # Resolve Material Request with prefix
                material_request = None
                material_request_item = None
                if row.get("material_request"):
                    material_request = resolve_local_doc("Material Request", row.material_request, company_abbr)
                    if material_request:
                        material_request_item = get_child_detail(
                            "Material Request Item",
                            material_request,
                            item_code,
                            remote_item_name=row.get("material_request_item")
                        )

                rfq.append("items", {
                    "item_code": item_code,
                    "item_name": row.item_name,
                    "description": row.description,
                    "qty": row.qty,
                    "uom": row.uom,
                    "warehouse": warehouse,
                    "schedule_date": row.schedule_date,
                    "supplier_part_no": row.supplier_part_no,
                    "custom_remote_id": row.get("custom_remote_id") or row.name,
                    "item_group": row.item_group,
                    "brand": row.brand,
                    "image": row.image,
                    "stock_uom": row.stock_uom,
                    "conversion_factor": row.conversion_factor or 1,
                    "stock_qty": row.stock_qty or row.qty,
                    "material_request": material_request,
                    "material_request_item": material_request_item,
                    "project_name": row.project_name,
                    "page_break": row.page_break,
                })

            # -------------------------------------------------
            # VALIDATE BEFORE INSERT (debug logging)
            # -------------------------------------------------
            _log(f"RFQ {target_name} pre-insert check",
                 f"Suppliers: {len(rfq.suppliers)}, Items: {len(rfq.items)}, "
                 f"Naming Series: {rfq.naming_series}, Company: {rfq.company}")

            # -------------------------------------------------
            # FLAGS
            # -------------------------------------------------
            rfq.flags.ignore_permissions = True
            rfq.flags.ignore_validate = True
            rfq.flags.ignore_mandatory = True
            rfq.flags.ignore_naming_series = True

            # -------------------------------------------------
            # INSERT
            # -------------------------------------------------
            rfq.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )
            frappe.db.commit()

            _log(f"RFQ {rfq.name} inserted successfully",
                 f"Docstatus after insert: {rfq.docstatus}, Status: {rfq.status}")

            # -------------------------------------------------
            # NAMING SAFETY NET
            # -------------------------------------------------
            if rfq.name != target_name:
                if not frappe.db.exists("Request for Quotation", target_name):
                    old_name = rfq.name
                    frappe.db.sql("""
                        UPDATE `tabRequest for Quotation`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, old_name))

                    for child in ["Request for Quotation Item", "Request for Quotation Supplier"]:
                        if frappe.db.exists("DocType", child):
                            frappe.db.sql(f"""
                                UPDATE `tab{child}`
                                SET parent = %s
                                WHERE parent = %s
                            """, (target_name, old_name))

                    frappe.db.commit()
                    rfq.name = target_name
                else:
                    _log(f"RFQ Sync - name collision",
                         f"Cannot rename {rfq.name} to {target_name}")

            # -------------------------------------------------
            # DOCSTATUS SYNC
            # -------------------------------------------------
            frappe.clear_document_cache("Request for Quotation", rfq.name)
            rfq = frappe.get_doc("Request for Quotation", rfq.name)

            _log(f"RFQ {rfq.name} pre-submit check",
                 f"External docstatus: {external.docstatus}, "
                 f"Current docstatus: {rfq.docstatus}, "
                 f"Suppliers count: {len(rfq.suppliers)}, "
                 f"Items count: {len(rfq.items)}")

            if external.docstatus == 1:
                # Try submit with full error capture
                try:
                    rfq.flags.ignore_permissions = True
                    rfq.flags.ignore_validate = True
                    rfq.flags.ignore_mandatory = True

                    # CRITICAL: Run validation first to see what would fail
                    validation_errors = []
                    try:
                        rfq.run_method("validate")
                    except Exception as ve:
                        validation_errors.append(f"validate: {str(ve)}")

                    if validation_errors:
                        _log(f"RFQ {rfq.name} validation errors before submit",
                             "\n".join(validation_errors))

                    rfq.submit()
                    frappe.db.commit()

                    _log(f"RFQ {rfq.name} submitted successfully", "")

                except Exception as submit_err:
                    tb = frappe.get_traceback()
                    _log(f"RFQ {rfq.name} SUBMIT FAILED - CRITICAL",
                         f"Error: {str(submit_err)}\n\nTraceback:\n{tb}")

                    # Try to identify the specific validation
                    err_str = str(submit_err).lower()
                    if "amend" in err_str:
                        _log(f"RFQ {rfq.name} - AMENDMENT ERROR",
                             "Document has amended_from or amendment issue")
                    elif "mandatory" in err_str:
                        _log(f"RFQ {rfq.name} - MANDATORY FIELD ERROR",
                             f"Missing mandatory field: {str(submit_err)}")
                    elif "naming" in err_str or "series" in err_str:
                        _log(f"RFQ {rfq.name} - NAMING SERIES ERROR",
                             f"Naming series issue: {str(submit_err)}")

                    results.append({
                        "name": rfq.name,
                        "status": "synced_draft",
                        "error": f"Submit failed: {str(submit_err)}"
                    })
                    continue

            elif external.docstatus == 2:
                try:
                    rfq.flags.ignore_permissions = True
                    rfq.flags.ignore_validate = True
                    rfq.flags.ignore_mandatory = True
                    rfq.submit()
                    frappe.db.commit()
                    rfq.cancel()
                    frappe.db.commit()
                except Exception as cancel_err:
                    _log(f"RFQ {rfq.name} cancel failed",
                         f"{str(cancel_err)}\n{frappe.get_traceback()}")
                    results.append({
                        "name": rfq.name,
                        "status": "synced_submitted",
                        "error": f"Cancel failed: {str(cancel_err)}"
                    })
                    continue

            # Update external doc
            external.db_set("remote_id", rfq.name)

            results.append({
                "name": rfq.name,
                "status": "synced",
                "docstatus": external.docstatus
            })

        except Exception as e:
            error = frappe.get_traceback()
            _log(f"RFQ Sync Error: {name}", error)
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results