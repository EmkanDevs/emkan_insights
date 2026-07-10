# import frappe
# import json

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }

# IGNORE_ITEM_FIELDS = {
#     "purchase_order_item",
#     "purchase_receipt_item",
#     "prevdoc_doctype",
#     "prevdoc_docname"
# }


# # -----------------------------------------------------
# # HELPER FUNCTIONS
# # -----------------------------------------------------

# def get_po_detail(purchase_order, item_code):

#     if not purchase_order:
#         return None

#     return frappe.db.get_value(
#         "Purchase Order Item",
#         {
#             "parent": purchase_order,
#             "item_code": item_code
#         },
#         "name"
#     )


# def get_pr_detail(purchase_receipt, item_code):

#     if not purchase_receipt:
#         return None

#     return frappe.db.get_value(
#         "Purchase Receipt Item",
#         {
#             "parent": purchase_receipt,
#             "item_code": item_code
#         },
#         "name"
#     )


# # ⭐ NEW FUNCTION (ONLY ADDITION)
# def clean_duplicate_pi_items(parent_name):
#     frappe.db.sql("""
#         DELETE t1 FROM `tabPurchase Invoice Item` t1
#         INNER JOIN `tabPurchase Invoice Item` t2
#         WHERE
#             t1.name > t2.name
#             AND t1.parent = t2.parent
#             AND t1.parent = %s
#             AND t1.item_code = t2.item_code
#             AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
#             AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
#     """, (parent_name,))


# # -----------------------------------------------------
# # MAIN SYNC
# # -----------------------------------------------------

# @frappe.whitelist()
# def sync_external_purchase_invoice_docs(source_doctype, names):

#     if isinstance(names, str):
#         names = json.loads(names)

#     results = []

#     for name in names:

#         try:

#             ext_pi = frappe.get_doc(source_doctype, name)
#             remote_id = ext_pi.name

#             # ------------------------------------------------
#             # BUILD TARGET NAME: {company_abbr}-{remote_id}
#             # Same pattern as Sales Order / Sales Invoice /
#             # Purchase Receipt sync
#             # ------------------------------------------------
#             company_abbr = frappe.db.get_value('Company', ext_pi.company, 'abbr') or ''

#             if not company_abbr:
#                 frappe.log_error(
#                     f"Purchase Invoice Sync: no company abbr found for company "
#                     f"'{ext_pi.company}' on {remote_id}. Name will be created "
#                     f"without a company prefix.",
#                     "ExternalPurchaseInvoice Sync - missing company abbr"
#                 )

#             target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

#             # -------------------------------------------------
#             # ALREADY SYNCED
#             # -------------------------------------------------

#             existing = frappe.db.get_value(
#                 "Purchase Invoice",
#                 {"remote_id": remote_id},
#                 "name"
#             )

#             if not existing:
#                 existing = frappe.db.exists("Purchase Invoice", target_name)

#             if existing:
#                 results.append({
#                     "name": existing,
#                     "status": "exists"
#                 })
#                 continue

#             pi = frappe.new_doc("Purchase Invoice")

#             # -------------------------------------------------
#             # SET NAME WITH COMPANY ABBR PREFIX
#             # Same pattern as Sales Order / Sales Invoice /
#             # Purchase Receipt
#             # -------------------------------------------------

#             pi.remote_id = remote_id
#             pi.name = target_name
#             pi.flags.name_set = True

#             if hasattr(pi, "source_site"):
#                 pi.source_site = ext_pi.source_site

#             # -------------------------------------------------
#             # COPY HEADER FIELDS
#             # -------------------------------------------------

#             for field, value in ext_pi.as_dict().items():

#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "taxes", "payment_schedule", "remote_id"]
#                     and hasattr(pi, field)
#                 ):
#                     pi.set(field, value)

#             # Fix currency validation
#             if not pi.currency:
#                 pi.currency = frappe.get_cached_value("Company", pi.company, "default_currency")

#             # -------------------------------------------------
#             # HANDLE RETURN
#             # -------------------------------------------------

#             if ext_pi.get("is_return"):

#                 if frappe.db.exists("Purchase Invoice", ext_pi.return_against):
#                     pi.is_return = 1
#                     pi.return_against = ext_pi.return_against
#                 else:
#                     pi.is_return = 0
#                     pi.return_against = None

#             # -------------------------------------------------
#             # ITEMS
#             # -------------------------------------------------

#             pi.set("items", [])

#             for row in ext_pi.items:

#                 item_row = {}

#                 for field, value in row.as_dict().items():

#                     if (
#                         field not in SYSTEM_FIELDS
#                         and field not in IGNORE_ITEM_FIELDS
#                     ):
#                         item_row[field] = value

#                 # PO mapping
#                 if row.purchase_order:
#                     item_row["purchase_order"] = row.purchase_order
#                     item_row["po_detail"] = get_po_detail(
#                         row.purchase_order,
#                         row.item_code
#                     )

#                 # PR mapping
#                 if row.purchase_receipt:
#                     item_row["purchase_receipt"] = row.purchase_receipt
#                     item_row["pr_detail"] = get_pr_detail(
#                         row.purchase_receipt,
#                         row.item_code
#                     )

#                 pi.append("items", item_row)

#             # -------------------------------------------------
#             # TAXES
#             # -------------------------------------------------

#             if ext_pi.get("taxes"):

#                 pi.set("taxes", [])

#                 for row in ext_pi.taxes:

#                     tax_row = {}

#                     for field, value in row.as_dict().items():

#                         if field not in SYSTEM_FIELDS:
#                             tax_row[field] = value

#                     pi.append("taxes", tax_row)

#             # -------------------------------------------------
#             # PAYMENT SCHEDULE
#             # -------------------------------------------------

#             if ext_pi.get("payment_schedule"):

#                 pi.set("payment_schedule", [])

#                 for row in ext_pi.payment_schedule:

#                     pay_row = {}

#                     for field, value in row.as_dict().items():

#                         if field not in SYSTEM_FIELDS:
#                             pay_row[field] = value

#                     pi.append("payment_schedule", pay_row)

#             # -------------------------------------------------
#             # FLAGS
#             # -------------------------------------------------

#             pi.flags.ignore_permissions = True
#             pi.flags.ignore_validate = True
#             pi.flags.ignore_mandatory = True
#             pi.flags.ignore_naming_series = True

#             # -------------------------------------------------
#             # INSERT
#             # -------------------------------------------------

#             pi.insert(
#                 ignore_permissions=True,
#                 ignore_links=True,
#                 ignore_mandatory=True
#             )

#             # ⭐ ONLY FIX (REMOVE DRAFT DUPLICATES)
#             clean_duplicate_pi_items(pi.name)
#             frappe.db.commit()

#             # -------------------------------------------------
#             # NAMING SAFETY NET: force back to target_name if
#             # Frappe renamed it, but only if that name isn't
#             # already taken by another doc.
#             # Same pattern as Sales Order / Sales Invoice /
#             # Purchase Receipt
#             # -------------------------------------------------

#             if pi.name != target_name:

#                 if not frappe.db.exists("Purchase Invoice", target_name):

#                     old_name = pi.name

#                     frappe.db.sql("""
#                         UPDATE `tabPurchase Invoice`
#                         SET name=%s
#                         WHERE name=%s
#                     """, (target_name, old_name))

#                     for child in [
#                         "Purchase Invoice Item",
#                         "Purchase Taxes and Charges",
#                         "Payment Schedule"
#                     ]:
#                         if frappe.db.exists("DocType", child):
#                             frappe.db.sql(f"""
#                                 UPDATE `tab{child}`
#                                 SET parent=%s
#                                 WHERE parent=%s
#                             """, (target_name, old_name))

#                     frappe.db.commit()

#                     pi.name = target_name

#                 else:
#                     frappe.log_error(
#                         f"Purchase Invoice Sync: could not rename {pi.name} to "
#                         f"{target_name} because that name already exists.",
#                         "ExternalPurchaseInvoice Sync - name collision"
#                     )

#             # -------------------------------------------------
#             # DOCSTATUS
#             # -------------------------------------------------

#             if ext_pi.docstatus == 1:
#                 pi.submit()

#             elif ext_pi.docstatus == 2:
#                 pi.submit()
#                 pi.cancel()

#             frappe.db.commit()

#             results.append({
#                 "name": pi.name,
#                 "status": "synced"
#             })

#         except Exception as e:

#             error = frappe.get_traceback()

#             frappe.log_error(
#                 title=f"Purchase Invoice Sync Error: {name}",
#                 message=error
#             )

#             results.append({
#                 "name": name,
#                 "status": "failed",
#                 "error": str(e)
#             })

#     return results



import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "purchase_order_item",
    "purchase_receipt_item",
    "prevdoc_doctype",
    "prevdoc_docname"
}


# -----------------------------------------------------
# SAFE LOG ERROR (fixes 140-char title truncation)
# -----------------------------------------------------

def _log(title, message):
    """Log error with guaranteed short title."""
    short_title = (title[:135] + "...") if len(title) > 135 else title
    frappe.log_error(title=short_title, message=message)


# -----------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------

def resolve_local_po(remote_or_local_po):
    if not remote_or_local_po:
        return None

    local_name = frappe.db.get_value(
        "Purchase Order", {"remote_id": remote_or_local_po}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return remote_or_local_po

    # Try company-prefixed
    company_abbr = frappe.db.get_default("company_abbr") or ""
    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return prefixed

    return None


def resolve_local_pr(remote_or_local_pr, company_abbr=None):
    if not remote_or_local_pr:
        return None

    local_name = frappe.db.get_value(
        "Purchase Receipt", {"remote_id": remote_or_local_pr}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Purchase Receipt", remote_or_local_pr):
        return remote_or_local_pr

    if company_abbr and not remote_or_local_pr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_pr}"
        if frappe.db.exists("Purchase Receipt", prefixed):
            return prefixed

    return None


def resolve_local_mr(remote_or_local_mr):
    if not remote_or_local_mr:
        return None

    local_name = frappe.db.get_value(
        "Material Request", {"remote_id": remote_or_local_mr}, "name"
    )
    if local_name:
        return local_name

    if frappe.db.exists("Material Request", remote_or_local_mr):
        return remote_or_local_mr

    company_abbr = frappe.db.get_default("company_abbr") or ""
    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed

    return None


def get_po_detail(purchase_order, item_code):
    if not purchase_order:
        return None
    return frappe.db.get_value(
        "Purchase Order Item",
        {"parent": purchase_order, "item_code": item_code},
        "name"
    )


def get_pr_detail(purchase_receipt, item_code, remote_pr_item=None):
    if not purchase_receipt:
        return None

    if remote_pr_item:
        exact = frappe.db.get_value(
            "Purchase Receipt Item",
            {"parent": purchase_receipt, "custom_remote_id": remote_pr_item},
            "name"
        )
        if exact:
            return exact

    return frappe.db.get_value(
        "Purchase Receipt Item",
        {"parent": purchase_receipt, "item_code": item_code},
        "name"
    )


def get_mr_detail(material_request, item_code):
    if not material_request:
        return None
    return frappe.db.get_value(
        "Material Request Item",
        {"parent": material_request, "item_code": item_code},
        "name"
    )


# -----------------------------------------------------
# SAFE DUPLICATE CLEANER
# -----------------------------------------------------

def clean_duplicate_pi_items(parent_name):
    if not parent_name:
        return

    total = frappe.db.count("Purchase Invoice Item", {"parent": parent_name})
    if total <= 1:
        return

    duplicates = frappe.db.sql("""
        SELECT t1.name as dup_name
        FROM `tabPurchase Invoice Item` t1
        INNER JOIN `tabPurchase Invoice Item` t2
            ON t1.parent = t2.parent
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
            AND t1.name > t2.name
        WHERE t1.parent = %s
    """, (parent_name,), as_dict=True)

    if not duplicates:
        return

    max_delete = int(total * 0.5)
    if len(duplicates) > max_delete:
        _log(
            title=f"PI dedup aborted for {parent_name}",
            message=f"Found {len(duplicates)} duplicates out of {total} items. "
                    f"Max allowed: {max_delete}. Manual review needed."
        )
        return

    for dup in duplicates:
        frappe.db.delete("Purchase Invoice Item", {"name": dup.dup_name})

    frappe.db.commit()


# -----------------------------------------------------
# CRITICAL FIX: Insert items one-by-one with error isolation
# -----------------------------------------------------

def _insert_pi_items_safely(pi_name, ext_pi, company_abbr=None):
    """
    Insert Purchase Invoice Items one at a time, catching each error
    so one bad item doesn't kill the entire batch.
    Returns: (success_count, error_list)
    """
    if not ext_pi.items:
        return 0, []

    success_count = 0
    errors = []

    for idx, row in enumerate(ext_pi.items, start=1):
        try:
            item_row = {"doctype": "Purchase Invoice Item"}

            # Copy fields safely
            for field, value in row.as_dict().items():
                if field in SYSTEM_FIELDS:
                    continue
                if field in IGNORE_ITEM_FIELDS:
                    continue
                # Skip link fields we'll resolve manually
                if field in {"purchase_order", "purchase_receipt",
                             "po_detail", "pr_detail", "material_request",
                             "material_request_item"}:
                    continue
                item_row[field] = value

            # Resolve PO
            if row.purchase_order:
                local_po = resolve_local_po(row.purchase_order)
                item_row["purchase_order"] = local_po
                item_row["po_detail"] = get_po_detail(local_po, row.item_code) if local_po else None
            else:
                item_row["purchase_order"] = None
                item_row["po_detail"] = None

            # Resolve PR
            if row.purchase_receipt:
                local_pr = resolve_local_pr(row.purchase_receipt, company_abbr)
                item_row["purchase_receipt"] = local_pr
                item_row["pr_detail"] = get_pr_detail(
                    local_pr,
                    row.item_code,
                    remote_pr_item=row.get("purchase_receipt_item")
                ) if local_pr else None
            else:
                item_row["purchase_receipt"] = None
                item_row["pr_detail"] = None

            # Resolve MR
            if row.get("material_request"):
                local_mr = resolve_local_mr(row.material_request)
                item_row["material_request"] = local_mr
                item_row["material_request_item"] = get_mr_detail(local_mr, row.item_code) if local_mr else None
            else:
                item_row["material_request"] = None
                item_row["material_request_item"] = None

            # Parent linkage
            item_row["parent"] = pi_name
            item_row["parentfield"] = "items"
            item_row["parenttype"] = "Purchase Invoice"
            item_row["idx"] = idx

            # Generate unique name to avoid any collision
            item_row["name"] = frappe.generate_hash(length=10)

            # CRITICAL: Use db_insert() directly to bypass ALL validation
            # that might fail on individual items
            child_doc = frappe.get_doc(item_row)
            child_doc.db_insert()

            success_count += 1

        except Exception as e:
            error_msg = f"Item {idx} (row name: {row.name}, item_code: {row.item_code}): {str(e)}"
            errors.append(error_msg)
            _log(
                title=f"PI item insert failed for {pi_name}",
                message=error_msg
            )

    if success_count > 0:
        frappe.db.commit()

    return success_count, errors


# -----------------------------------------------------
# MAIN SYNC
# -----------------------------------------------------

@frappe.whitelist()
def sync_external_purchase_invoice_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:

        try:

            # Force fresh transaction snapshot
            frappe.db.commit()

            ext_pi = frappe.get_doc(source_doctype, name)
            remote_id = ext_pi.name

            # Defensive reload if items empty
            if not ext_pi.items:
                frappe.clear_document_cache(source_doctype, name)
                ext_pi.reload()

                if not ext_pi.items:
                    actual_count = frappe.db.count(f"{source_doctype} Item", {"parent": name})
                    if actual_count > 0:
                        ext_pi = frappe.get_doc(source_doctype, name)
                        for field in ext_pi.meta.get_table_fields():
                            ext_pi.load_children(field.fieldname)

                    if not ext_pi.items:
                        _log(
                            title="PI Sync - external doc has 0 items",
                            message=f"External PI {name} has 0 items even after reload. "
                                    f"Proceeding with empty items table."
                        )

            # Build target name
            company_abbr = frappe.db.get_value('Company', ext_pi.company, 'abbr') or ''

            if not company_abbr:
                _log(
                    title="PI Sync - missing company abbr",
                    message=f"No company abbr for '{ext_pi.company}' on {remote_id}."
                )

            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # Check already synced
            existing = frappe.db.get_value(
                "Purchase Invoice", {"remote_id": remote_id}, "name"
            )

            if not existing:
                existing = frappe.db.exists("Purchase Invoice", target_name)

            if existing:
                results.append({
                    "name": existing,
                    "status": "exists"
                })
                continue

            pi = frappe.new_doc("Purchase Invoice")

            # Set name BEFORE anything else
            pi.remote_id = remote_id
            pi.name = target_name
            pi.flags.name_set = True

            if hasattr(pi, "source_site"):
                pi.source_site = ext_pi.source_site

            # Copy header fields
            for field, value in ext_pi.as_dict().items():
                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "payment_schedule", "remote_id"]
                    and hasattr(pi, field)
                ):
                    pi.set(field, value)

            # Fix currency
            if not pi.currency:
                pi.currency = frappe.get_cached_value(
                    "Company", pi.company, "default_currency"
                )

            # Handle return
            if ext_pi.get("is_return"):
                if frappe.db.exists("Purchase Invoice", ext_pi.return_against):
                    pi.is_return = 1
                    pi.return_against = ext_pi.return_against
                else:
                    pi.is_return = 0
                    pi.return_against = None

            # -------------------------------------------------
            # ITEMS: Use safe one-by-one insert instead of append+insert
            # -------------------------------------------------

            # Clear items on parent (won't be saved via normal insert)
            pi.set("items", [])

            # -------------------------------------------------
            # TAXES
            # -------------------------------------------------

            if ext_pi.get("taxes"):
                pi.set("taxes", [])
                for row in ext_pi.taxes:
                    tax_row = {"doctype": "Purchase Taxes and Charges"}
                    for field, value in row.as_dict().items():
                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value
                    pi.append("taxes", tax_row)

            # -------------------------------------------------
            # PAYMENT SCHEDULE (rebuild with fresh names)
            # -------------------------------------------------

            if ext_pi.get("payment_schedule"):
                pi.set("payment_schedule", [])
                for idx, row in enumerate(ext_pi.payment_schedule, start=1):
                    pay_row = {
                        "doctype": "Payment Schedule",
                        "idx": idx,
                        "payment_term": row.payment_term,
                        "description": row.description,
                        "due_date": row.due_date,
                        "invoice_portion": row.invoice_portion,
                        "discount": row.discount,
                        "payment_amount": row.payment_amount,
                        "base_payment_amount": row.base_payment_amount,
                    }
                    pi.append("payment_schedule", pay_row)

            # -------------------------------------------------
            # FLAGS
            # -------------------------------------------------

            pi.flags.ignore_permissions = True
            pi.flags.ignore_validate = True
            pi.flags.ignore_mandatory = True
            pi.flags.ignore_naming_series = True

            # -------------------------------------------------
            # INSERT PARENT ONLY (no items yet)
            # -------------------------------------------------

            pi.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            frappe.db.commit()

            # -------------------------------------------------
            # NOW INSERT ITEMS ONE-BY-ONE WITH ERROR ISOLATION
            # -------------------------------------------------

            success_count, item_errors = _insert_pi_items_safely(pi.name, ext_pi, company_abbr)

            if item_errors:
                _log(
                    title=f"PI {pi.name} - {len(item_errors)} item errors",
                    message="Item insert errors:\n" + "\n".join(item_errors)
                )

            # -------------------------------------------------
            # VERIFY
            # -------------------------------------------------

            actual_item_count = frappe.db.count(
                "Purchase Invoice Item", {"parent": pi.name}
            )
            expected_item_count = len(ext_pi.get("items") or [])

            if actual_item_count != expected_item_count:
                _log(
                    title=f"PI Sync - items mismatch for {pi.name}",
                    message=f"Expected {expected_item_count} items, "
                            f"found {actual_item_count} after insert. "
                            f"Successfully inserted: {success_count}, "
                            f"Errors: {len(item_errors)}."
                )

            # Clean duplicates
            clean_duplicate_pi_items(pi.name)
            frappe.db.commit()

            # -------------------------------------------------
            # NAMING SAFETY NET
            # -------------------------------------------------

            if pi.name != target_name:

                if not frappe.db.exists("Purchase Invoice", target_name):

                    old_name = pi.name

                    frappe.db.sql("""
                        UPDATE `tabPurchase Invoice`
                        SET name=%s
                        WHERE name=%s
                    """, (target_name, old_name))

                    for child in [
                        "Purchase Invoice Item",
                        "Purchase Taxes and Charges",
                        "Payment Schedule"
                    ]:
                        if frappe.db.exists("DocType", child):
                            frappe.db.sql(f"""
                                UPDATE `tab{child}`
                                SET parent=%s
                                WHERE parent=%s
                            """, (target_name, old_name))

                    frappe.db.commit()
                    pi.name = target_name

                else:
                    _log(
                        title="PI Sync - name collision",
                        message=f"Cannot rename {pi.name} to {target_name} - already exists."
                    )

           
            
            frappe.clear_document_cache("Purchase Invoice", pi.name)
            pi = frappe.get_doc("Purchase Invoice", pi.name)

            
            pi.flags.ignore_permissions = True
            pi.flags.ignore_validate = True
            pi.flags.ignore_mandatory = True
            pi.flags.ignore_naming_series = True

            # -------------------------------------------------
            # DOCSTATUS
            # -------------------------------------------------

            if ext_pi.docstatus == 1:
                pi.submit()

            elif ext_pi.docstatus == 2:
                pi.submit()
                pi.cancel()

            frappe.db.commit()

            results.append({
                "name": pi.name,
                "status": "synced",
                "items_synced": actual_item_count,
                "items_expected": expected_item_count
            })

        except Exception as e:

            error = frappe.get_traceback()

            _log(
                title=f"Purchase Invoice Sync Error: {name}",
                message=error
            )

            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    return results