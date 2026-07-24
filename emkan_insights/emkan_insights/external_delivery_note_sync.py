import frappe
import json
import types
from frappe.utils import cint, flt

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "name"
}

EXPLICITLY_HANDLED_HEADER_FIELDS = {
    "items", "taxes", "remote_id", "custom_remote_id", "customer",
    "is_return", "return_against"
}

LOCAL_ITEM_PARENTTYPE = "Delivery Note"


# ==========================================================
# NEGATIVE STOCK BYPASS
# ==========================================================

def _bypass_negative_stock_validation(doc):
    """
    Temporarily bypass negative stock validation for this specific document instance.
    Safe for concurrent requests because it only patches the instance, not the class.
    """
    original_update_sl = doc.update_stock_ledger

    def patched_update_sl(self):
        original_make_sl = self.make_sl_entries
        
        def patched_make_sl(sl_entries, allow_negative_stock=False, via_landed_cost_voucher=False):
            # Force allow_negative_stock to True
            original_make_sl(sl_entries, allow_negative_stock=True, via_landed_cost_voucher=via_landed_cost_voucher)
            
        self.make_sl_entries = patched_make_sl
        original_update_sl(self)
        self.make_sl_entries = original_make_sl

    doc.update_stock_ledger = types.MethodType(patched_update_sl, doc)


# ==========================================================
# GL ENTRY DIAGNOSTIC LOGGER
# ==========================================================

def _verify_and_log_gl_entries(doc, action_taken):
    """
    Verify if GL entries were actually created for a document.
    If missing, logs a detailed Error Log explaining exactly why.
    """
    if not doc or not doc.name:
        return

    enable_perpetual = frappe.db.get_value("Company", doc.company, "enable_perpetual_inventory")
    
    if not cint(enable_perpetual):
        frappe.log_error(
            title=f"DN Sync GL Skip: Perpetual Inventory Off ({doc.name})",
            message=(
                f"Action: {action_taken}\n\n"
                f"Company '{doc.company}' does not have 'Enable Perpetual Inventory' checked. "
                f"Delivery Notes do NOT create GL entries when perpetual inventory is disabled."
            )
        )
        return

    if action_taken == "Cancelled":
        gl_count = frappe.db.count("GL Entry", {
            "voucher_type": "Delivery Note",
            "voucher_no": doc.name,
            "is_cancelled": 1
        })
        if gl_count == 0:
            frappe.log_error(
                title=f"DN Sync GL Missing: Cancel entries missing for {doc.name}",
                message=f"Document was cancelled, but 0 cancellation GL entries were found."
            )
        return

    gl_count = frappe.db.count("GL Entry", {
        "voucher_type": "Delivery Note",
        "voucher_no": doc.name,
        "is_cancelled": 0
    })

    if gl_count > 0:
        return

    items_summary = []
    for item in doc.get("items", []):
        items_summary.append(
            f"- Item: {item.item_code} | Qty: {flt(item.qty)} | Rate: {flt(item.rate)} | "
            f"Amount: {flt(item.amount)} | Warehouse: {item.warehouse} | "
            f"Expense Account: {item.get('expense_account', 'NOT SET')}"
        )

    diag_message = (
        f"Action: {action_taken}\n"
        f"Company: {doc.company}\n"
        f"Is Return: {doc.is_return}\n"
        f"Total Qty: {flt(doc.total_qty)}\n"
        f"Net Total: {flt(doc.net_total)}\n"
        f"Items Details:\n" + ("\n".join(items_summary) if items_summary else "No items found!")
    )

    frappe.log_error(
        title=f"DN Sync GL Missing: 0 GL Entries for {doc.name}",
        message=diag_message
    )


# ==========================================================
# DUPLICATE CLEANUP
# ==========================================================

def clean_duplicate_dn_items(parent_name):
    frappe.db.sql("""
        DELETE t1 FROM `tabDelivery Note Item` t1
        INNER JOIN `tabDelivery Note Item` t2
        WHERE
            t1.name > t2.name
            AND t1.parent = t2.parent
            AND t1.parent = %s
            AND t1.parenttype = %s
            AND t2.parenttype = %s
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
            AND IFNULL(t1.against_sales_order, '') = IFNULL(t2.against_sales_order, '')
            AND IFNULL(t1.custom_remote_id, '') = IFNULL(t2.custom_remote_id, '')
    """, (parent_name, LOCAL_ITEM_PARENTTYPE, LOCAL_ITEM_PARENTTYPE))


def _purge_orphan_local_dn_children(parent_name):
    if not parent_name:
        return
    if frappe.db.exists("Delivery Note", parent_name):
        return
    for df in frappe.get_meta("Delivery Note").get_table_fields():
        frappe.db.delete(df.options, {
            "parent": parent_name,
            "parenttype": LOCAL_ITEM_PARENTTYPE,
        })


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


def get_child_detail(doctype, parent, item_code, remote_child_id=None, parenttype=None):
    if parenttype is None:
        parenttype = doctype[:-5] if doctype.endswith(" Item") else None

    if remote_child_id:
        if parent:
            filters = {"custom_remote_id": remote_child_id, "parent": parent}
            if parenttype:
                filters["parenttype"] = parenttype
            match = frappe.db.get_value(doctype, filters, "name")
            if match:
                return match

        cand_filters = {"custom_remote_id": remote_child_id}
        if parenttype:
            cand_filters["parenttype"] = parenttype
        candidates = frappe.get_all(
            doctype, filters=cand_filters, fields=["name", "parent"], limit=20
        )
        for c in candidates:
            if c.parent and (not parenttype or frappe.db.exists(parenttype, c.parent)):
                return c.name

    if not parent:
        return None

    filters = {"parent": parent, "item_code": item_code}
    if parenttype:
        filters["parenttype"] = parenttype
    return frappe.db.get_value(doctype, filters, "name")


def _resolve_customer(customer_ref, company_abbr):
    if not customer_ref:
        return None

    resolved = resolve_local_doc("Customer", customer_ref, company_abbr)
    if not resolved:
        frappe.log_error(
            title=f"Delivery Note Sync - Customer '{customer_ref}' unresolved",
            message="Customer could not be resolved to a local Customer. Leaving field blank so the DN still syncs."
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
# MAIN ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_delivery_note_docs(source_doctype, names):
    """
    Main sync entry point.
    """
    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_dn = frappe.get_doc(source_doctype, name)
            remote_id = ext_dn.name

            company_abbr = frappe.db.get_value("Company", ext_dn.company, "abbr") or ""

            if not company_abbr:
                frappe.log_error(
                    f"Delivery Note Sync: no company abbr found for company "
                    f"'{ext_dn.company}' on {remote_id}. Name will be created "
                    f"without a company prefix.",
                    "ExternalDeliveryNote Sync - missing company abbr"
                )

            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            remote_id_field = None
            dn_meta = frappe.get_meta("Delivery Note")
            for candidate in ("custom_remote_id", "remote_id"):
                if dn_meta.has_field(candidate):
                    remote_id_field = candidate
                    break

            if not remote_id_field:
                frappe.throw(
                    "Delivery Note has neither 'custom_remote_id' nor "
                    "'remote_id' field - cannot track sync identity."
                )

            existing_dn = frappe.db.get_value(
                "Delivery Note",
                {remote_id_field: remote_id},
                "name"
            ) or (target_name if frappe.db.exists("Delivery Note", target_name) else None)

            if existing_dn:
                results.append({
                    "name": existing_dn,
                    "status": "exists"
                })
                continue

            dn = frappe.new_doc("Delivery Note")

            dn.set(remote_id_field, remote_id)
            dn.name = target_name
            dn.flags.name_set = True

            dn.flags.ignore_permissions = True
            dn.flags.ignore_naming_series = True

            CALCULATED_FIELDS = {
                "total", "base_total", "net_total", "base_net_total",
                "grand_total", "base_grand_total", "rounded_total",
                "base_rounded_total", "total_taxes_and_charges",
                "base_total_taxes_and_charges", "outstanding_amount", "status",
            }
            for field, value in ext_dn.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in EXPLICITLY_HANDLED_HEADER_FIELDS
                    and hasattr(dn, field)
                ):
                    dn.set(field, value)

            dn.customer = _resolve_customer(ext_dn.get("customer"), company_abbr)

            if ext_dn.get("is_return"):
                local_return_against = resolve_local_doc(
                    "Delivery Note", ext_dn.get("return_against"), company_abbr
                ) if ext_dn.get("return_against") else None

                if local_return_against:
                    dn.is_return = 1
                    dn.return_against = local_return_against
                else:
                    dn.is_return = 0
                    dn.return_against = None

            dn.set("items", [])
            deduped_items = _dedupe_source_items(ext_dn.items)

            for row in deduped_items:
                item_row = {}

                for field, value in row.as_dict().items():
                    if field not in SYSTEM_FIELDS and field not in IGNORE_ITEM_FIELDS:
                        item_row[field] = value

                so_ref = row.get("against_sales_order") or row.get("sales_order")
                local_so = resolve_local_doc("Sales Order", so_ref, company_abbr) if so_ref else None

                local_so_detail = get_child_detail(
                    "Sales Order Item",
                    local_so,
                    row.item_code,
                    remote_child_id=row.get("so_detail"),
                    parenttype="Sales Order",
                )

                if not local_so and local_so_detail:
                    local_so = frappe.db.get_value("Sales Order Item", local_so_detail, "parent")

                item_row["against_sales_order"] = local_so
                item_row["so_detail"] = local_so_detail

                item_row.pop("sales_order", None)
                item_row.pop("sales_order_item", None)
                item_row.pop("prevdoc_doctype", None)
                item_row.pop("prevdoc_docname", None)

                if frappe.get_meta("Delivery Note Item").has_field("custom_remote_id"):
                    item_row["custom_remote_id"] = row.name

                if not item_row.get("warehouse"):
                    item_row["warehouse"] = _resolve_default_warehouse(ext_dn.company)

                if item_row.get("item_code") and item_row.get("uom"):
                    _ensure_item_uom(item_row["item_code"], item_row["uom"])

                dn.append("items", item_row)

            if hasattr(ext_dn, "taxes"):
                dn.set("taxes", [])
                for row in ext_dn.taxes:
                    tax_row = {}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value
                    dn.append("taxes", tax_row)

            _purge_orphan_local_dn_children(target_name)

            dn.set_missing_values()
            dn.calculate_taxes_and_totals()
            
            dn.insert(ignore_permissions=True)

            clean_duplicate_dn_items(dn.name)

            if dn.name != target_name:
                if not frappe.db.exists("Delivery Note", target_name):
                    old_name = dn.name

                    frappe.db.sql("""
                        UPDATE `tabDelivery Note` SET name = %s WHERE name = %s
                    """, (target_name, old_name))

                    frappe.db.sql("""
                        UPDATE `tabDelivery Note Item` SET parent = %s
                        WHERE parent = %s AND parenttype = %s
                    """, (target_name, old_name, LOCAL_ITEM_PARENTTYPE))

                    if frappe.db.exists("DocType", "Sales Taxes and Charges"):
                        frappe.db.sql("""
                            UPDATE `tabSales Taxes and Charges` SET parent = %s
                            WHERE parent = %s AND parenttype = %s
                        """, (target_name, old_name, LOCAL_ITEM_PARENTTYPE))

                    dn.name = target_name
                else:
                    frappe.log_error(
                        f"Delivery Note Sync: could not rename {dn.name} to "
                        f"{target_name} because that name already exists.",
                        "ExternalDeliveryNote Sync - name collision"
                    )

            # ------------------------------------------------
            # DOCSTATUS SYNC (ORM submit / cancel)
            # ------------------------------------------------
            if ext_dn.docstatus == 1:
                # BYPASS: Prevent Insufficient Stock errors during sync
                _bypass_negative_stock_validation(dn)
                
                dn.submit()
                
                dn.reload()
                _verify_and_log_gl_entries(dn, "Submitted")

            elif ext_dn.docstatus == 2:
                # BYPASS: Prevent Insufficient Stock errors during sync
                _bypass_negative_stock_validation(dn)
                
                dn.submit()
                dn.cancel()
                
                dn.reload()
                _verify_and_log_gl_entries(dn, "Cancelled")

            results.append({
                "name": dn.name,
                "status": "synced"
            })

        except Exception as e:
            error = frappe.get_traceback()

            frappe.log_error(
                title=f"Delivery Note Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results