import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

# ⭐ FIX: Added purchase_invoice_item and purchase_receipt_detail to ignore list
IGNORE_ITEM_FIELDS = {
    "purchase_order_item",
    "purchase_invoice_item",
    "purchase_receipt_detail",
    "prevdoc_doctype",
    "prevdoc_docname"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


def _get_table_fieldnames(meta):
    return {df.fieldname for df in meta.get_table_fields()}


def _apply_sync_flags(doc):
    """Apply all necessary flags for sync operations"""
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_validate = True
    doc.flags.ignore_naming_series = True
    frappe.flags.ignore_stock_validation = True


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
        target_doc.append(
            table_fieldname,
            _copy_child_row_fields(row, target_df.options)
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
# PURCHASE ORDER RESOLUTION (Existing)
# =============================================================================
def resolve_local_po(remote_or_local_po, company_abbr=None):
    if not remote_or_local_po:
        return None
    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return prefixed
    local_name = frappe.db.get_value(
        "Purchase Order", {"remote_id": remote_or_local_po}, "name"
    )
    if local_name:
        return local_name
    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return remote_or_local_po
    return None


def get_po_detail(purchase_order, item_code, remote_po_item=None):
    if not purchase_order:
        return None

    if remote_po_item:
        if frappe.db.exists(
            "Purchase Order Item",
            {"name": remote_po_item, "parent": purchase_order}
        ):
            return remote_po_item

        poi_meta = frappe.get_meta("Purchase Order Item")
        if poi_meta.has_field("custom_remote_id"):
            local_po_item = frappe.db.get_value(
                "Purchase Order Item",
                {"parent": purchase_order, "custom_remote_id": remote_po_item},
                "name"
            )
            if local_po_item:
                return local_po_item

    return frappe.db.get_value(
        "Purchase Order Item",
        {"parent": purchase_order, "item_code": item_code},
        "name"
    )


# =============================================================================
# MATERIAL REQUEST RESOLUTION (Existing)
# =============================================================================
def resolve_local_mr(remote_or_local_mr, company_abbr=None):
    if not remote_or_local_mr:
        return None
    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed
    local_name = frappe.db.get_value(
        "Material Request", {"remote_id": remote_or_local_mr}, "name"
    )
    if local_name:
        return local_name
    if frappe.db.exists("Material Request", remote_or_local_mr):
        return remote_or_local_mr
    return None


def get_mr_detail(material_request, item_code):
    if not material_request:
        return None
    return frappe.db.get_value(
        "Material Request Item",
        {"parent": material_request, "item_code": item_code},
        "name"
    )


# =============================================================================
# ⭐ NEW: PURCHASE INVOICE RESOLUTION (Same pattern as MR)
# =============================================================================
def resolve_local_pi(remote_or_local_pi, company_abbr=None):
    """Resolve remote Purchase Invoice to local Purchase Invoice"""
    if not remote_or_local_pi:
        return None
    
    # Try with company prefix
    if company_abbr and not remote_or_local_pi.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pi}"
        if frappe.db.exists("Purchase Invoice", prefixed):
            return prefixed
    
    # Try by remote_id field
    pi_meta = frappe.get_meta("Purchase Invoice")
    for candidate in ("custom_remote_id", "remote_id"):
        if pi_meta.has_field(candidate):
            local_name = frappe.db.get_value(
                "Purchase Invoice", {candidate: remote_or_local_pi}, "name"
            )
            if local_name:
                return local_name
            break
    
    # Try direct name match
    if frappe.db.exists("Purchase Invoice", remote_or_local_pi):
        return remote_or_local_pi
    
    return None


def get_pi_detail(purchase_invoice, item_code, remote_pi_item=None):
    """Get local Purchase Invoice Item detail"""
    if not purchase_invoice:
        return None

    # First try by remote item name/ID
    if remote_pi_item:
        # Direct match
        if frappe.db.exists(
            "Purchase Invoice Item",
            {"name": remote_pi_item, "parent": purchase_invoice}
        ):
            return remote_pi_item

        # Try by custom_remote_id field
        pii_meta = frappe.get_meta("Purchase Invoice Item")
        if pii_meta.has_field("custom_remote_id"):
            local_pi_item = frappe.db.get_value(
                "Purchase Invoice Item",
                {"parent": purchase_invoice, "custom_remote_id": remote_pi_item},
                "name"
            )
            if local_pi_item:
                return local_pi_item

    # Fallback: match by item_code
    return frappe.db.get_value(
        "Purchase Invoice Item",
        {"parent": purchase_invoice, "item_code": item_code},
        "name"
    )


# =============================================================================
# ⭐ NEW: PURCHASE RECEIPT RESOLUTION (Same pattern as MR)
# =============================================================================
def resolve_local_pr_for_item(remote_or_local_pr, company_abbr=None):
    """Resolve remote Purchase Receipt to local Purchase Receipt (for item linking)"""
    if not remote_or_local_pr:
        return None
    
    # Try with company prefix
    if company_abbr and not remote_or_local_pr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pr}"
        if frappe.db.exists("Purchase Receipt", prefixed):
            return prefixed
    
    # Try by remote_id field
    pr_meta = frappe.get_meta("Purchase Receipt")
    for candidate in ("custom_remote_id", "remote_id"):
        if pr_meta.has_field(candidate):
            local_name = frappe.db.get_value(
                "Purchase Receipt", {candidate: remote_or_local_pr}, "name"
            )
            if local_name:
                return local_name
            break
    
    # Try direct name match
    if frappe.db.exists("Purchase Receipt", remote_or_local_pr):
        return remote_or_local_pr
    
    return None


def get_pr_detail_for_item(purchase_receipt, item_code, remote_pr_item=None):
    """Get local Purchase Receipt Item detail (for PI item linking)"""
    if not purchase_receipt:
        return None

    # First try by remote item name/ID
    if remote_pr_item:
        # Direct match
        if frappe.db.exists(
            "Purchase Receipt Item",
            {"name": remote_pr_item, "parent": purchase_receipt}
        ):
            return remote_pr_item

        # Try by custom_remote_id field
        pri_meta = frappe.get_meta("Purchase Receipt Item")
        if pri_meta.has_field("custom_remote_id"):
            local_pr_item = frappe.db.get_value(
                "Purchase Receipt Item",
                {"parent": purchase_receipt, "custom_remote_id": remote_pr_item},
                "name"
            )
            if local_pr_item:
                return local_pr_item

    # Fallback: match by item_code
    return frappe.db.get_value(
        "Purchase Receipt Item",
        {"parent": purchase_receipt, "item_code": item_code},
        "name"
    )


def _set_docstatus_directly(doctype, docname, docstatus):
    """Directly set docstatus in database without going through submit/cancel workflow."""
    frappe.db.sql(
        """UPDATE `tab{0}` SET docstatus = %s, modified = NOW(), modified_by = %s WHERE name = %s""".format(doctype),
        (docstatus, frappe.session.user, docname)
    )
    frappe.db.commit()


@frappe.whitelist()
def sync_external_purchase_receipt_docs(source_doctype, names):
    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_pr = frappe.get_doc(source_doctype, name)
            remote_id = ext_pr.name

            source_docstatus = _get_source_docstatus(ext_pr)

            company_abbr = frappe.db.get_value('Company', ext_pr.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Purchase Receipt Sync: no company abbr found for company "
                    f"'{ext_pr.company}' on {remote_id}.",
                    "ExternalPurchaseReceipt Sync - missing company abbr"
                )

            target_name = (
                f"{company_abbr}-{remote_id}"
                if company_abbr and not remote_id.startswith(f"{company_abbr}-")
                else remote_id
            )

            remote_id_field = None
            pr_meta = frappe.get_meta("Purchase Receipt")
            for candidate in ("custom_remote_id", "remote_id"):
                if pr_meta.has_field(candidate):
                    remote_id_field = candidate
                    break

            if not remote_id_field:
                frappe.throw(
                    "Purchase Receipt has neither 'custom_remote_id' nor "
                    "'remote_id' field - cannot track sync identity."
                )

            existing_pr = frappe.db.get_value(
                "Purchase Receipt",
                {remote_id_field: remote_id},
                ["name", "docstatus"]
            )

            if existing_pr:
                existing_name, existing_docstatus = existing_pr
                
                if source_docstatus != existing_docstatus:
                    try:
                        if source_docstatus == 2:
                            _set_docstatus_directly("Purchase Receipt", existing_name, 2)
                        elif source_docstatus == 1 and existing_docstatus == 0:
                            doc = frappe.get_doc("Purchase Receipt", existing_name)
                            _apply_sync_flags(doc)
                            doc.submit()
                            frappe.db.commit()
                        elif source_docstatus == 0 and existing_docstatus == 1:
                            frappe.log_error(
                                title=f"Cannot unsubmit Purchase Receipt: {existing_name}",
                                message=f"Source docstatus is 0 (Draft) but local is 1 (Submitted). Cannot unsubmit."
                            )
                    except Exception as status_error:
                        frappe.log_error(
                            title=f"Purchase Receipt Status Update Error: {existing_name}",
                            message=f"Failed to update docstatus: {frappe.get_traceback()}"
                        )
                
                results.append({
                    "name": existing_name,
                    "status": "exists"
                })
                continue

            pr = frappe.new_doc("Purchase Receipt")

            pr.set(remote_id_field, remote_id)
            pr.name = target_name
            pr.flags.name_set = True

            _apply_sync_flags(pr)

            if hasattr(pr, "source_site"):
                pr.source_site = ext_pr.source_site

            # -----------------------------
            # COPY MAIN FIELDS
            # -----------------------------
            pr_table_fields = _get_table_fieldnames(pr_meta)
            for field, value in ext_pr.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in pr_table_fields
                    # ⭐ FIX: Excluded "return_against" to prevent "not found" error
                    and field not in ["remote_id", "custom_remote_id", "status", "return_against"]
                    and hasattr(pr, field)
                ):
                    pr.set(field, value)

            # -----------------------------
            # ⭐ FIX: RESOLVE RETURN AGAINST PR NAMING
            # -----------------------------
            if ext_pr.get("return_against"):
                pr.return_against = resolve_local_pr_for_item(ext_pr.return_against, company_abbr)
            else:
                pr.return_against = None

            # -----------------------------
            # ITEMS
            # -----------------------------
            pr.set("items", [])

            # ⭐ Get target item meta once for efficiency
            pr_item_meta = frappe.get_meta("Purchase Receipt Item")
            has_custom_remote_id = pr_item_meta.has_field("custom_remote_id")

            for row in ext_pr.items:
                item_row = _copy_child_row_fields(
                    row,
                    "Purchase Receipt Item",
                    extra_ignore=IGNORE_ITEM_FIELDS
                )

                if has_custom_remote_id:
                    item_row["custom_remote_id"] = row.name

                # ==============================
                # PURCHASE ORDER LINKING (Existing)
                # ==============================
                if row.get("purchase_order"):
                    local_po = resolve_local_po(row.purchase_order, company_abbr)
                    item_row["purchase_order"] = local_po
                    item_row["purchase_order_item"] = get_po_detail(
                        local_po,
                        row.item_code,
                        row.get("purchase_order_item")
                    ) if local_po else None
                else:
                    item_row["purchase_order"] = None
                    item_row["purchase_order_item"] = None

                # ==============================
                # MATERIAL REQUEST LINKING (Existing)
                # ==============================
                if row.get("material_request"):
                    local_mr = resolve_local_mr(row.material_request, company_abbr)
                    item_row["material_request"] = local_mr
                    item_row["material_request_item"] = get_mr_detail(
                        local_mr, row.item_code
                    ) if local_mr else None
                else:
                    item_row["material_request"] = None
                    item_row["material_request_item"] = None

                # ==============================
                # ⭐ NEW: PURCHASE INVOICE LINKING (Same pattern as MR)
                # ==============================
                if row.get("purchase_invoice"):
                    local_pi = resolve_local_pi(row.purchase_invoice, company_abbr)
                    item_row["purchase_invoice"] = local_pi
                    item_row["purchase_invoice_item"] = get_pi_detail(
                        local_pi,
                        row.item_code,
                        row.get("purchase_invoice_item")
                    ) if local_pi else None
                else:
                    item_row["purchase_invoice"] = None
                    item_row["purchase_invoice_item"] = None

                # ==============================
                # ⭐ NEW: PURCHASE RECEIPT LINKING (For return/debit notes)
                # ==============================
                if row.get("purchase_receipt"):
                    local_pr_ref = resolve_local_pr_for_item(row.purchase_receipt, company_abbr)
                    item_row["purchase_receipt"] = local_pr_ref
                    item_row["purchase_receipt_detail"] = get_pr_detail_for_item(
                        local_pr_ref,
                        row.item_code,
                        row.get("purchase_receipt_detail")
                    ) if local_pr_ref else None
                else:
                    item_row["purchase_receipt"] = None
                    item_row["purchase_receipt_detail"] = None

                if not item_row.get("warehouse"):
                    item_row["warehouse"] = DEFAULT_WAREHOUSE

                pr.append("items", item_row)

            # -----------------------------
            # TAXES
            # -----------------------------
            _append_matching_child_table(pr, ext_pr, "taxes")

            # -----------------------------
            # OTHER CHILD TABLES
            # -----------------------------
            for table_fieldname in ("pricing_rules", "supplied_items"):
                _append_matching_child_table(pr, ext_pr, table_fieldname)

            # -----------------------------
            # INSERT
            # -----------------------------
            pr.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            clean_duplicate_pr_items(pr.name)
            frappe.db.commit()

            # -----------------------------
            # NAMING SAFETY NET
            # -----------------------------
            final_name = pr.name
            if pr.name != target_name:
                if not frappe.db.exists("Purchase Receipt", target_name):
                    old_name = pr.name

                    frappe.db.sql("""
                        UPDATE `tabPurchase Receipt`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, old_name))

                    for df in pr_meta.get_table_fields():
                        child_table = df.options
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""
                                UPDATE `tab{0}`
                                SET parent = %s
                                WHERE parent = %s
                            """.format(child_table), (target_name, old_name))

                    frappe.db.commit()
                    final_name = target_name
                    
                else:
                    frappe.log_error(
                        f"Purchase Receipt Sync: could not rename {pr.name} to "
                        f"{target_name} because that name already exists.",
                        "ExternalPurchaseReceipt Sync - name collision"
                    )

            # -----------------------------
            # DOCSTATUS SYNC
            # -----------------------------
            if source_docstatus == 2:
                _set_docstatus_directly("Purchase Receipt", final_name, 2)
                
            elif source_docstatus == 1:
                try:
                    pr = frappe.get_doc("Purchase Receipt", final_name)
                    _apply_sync_flags(pr)
                    pr.submit()
                    frappe.db.commit()
                except Exception as submit_error:
                    frappe.log_error(
                        title=f"Purchase Receipt Submit Error: {final_name}",
                        message=f"Source docstatus was 1 (Submitted) but submit failed: {frappe.get_traceback()}"
                    )

            results.append({
                "name": final_name,
                "status": "synced"
            })

        except Exception as e:
            error = frappe.get_traceback()
            frappe.log_error(
                title=f"Purchase Receipt Sync Error: {name}",
                message=error
            )
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results