# import frappe
# import json

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }

# IGNORE_ITEM_FIELDS = {
#     "purchase_order_item",
#     "prevdoc_doctype",
#     "prevdoc_docname"
# }

# DEFAULT_WAREHOUSE = "Stores - IMC"


# # ⭐ ONLY ADDITION (duplicate cleanup)
# def clean_duplicate_pr_items(parent_name):
#     frappe.db.sql("""
#         DELETE t1 FROM `tabPurchase Receipt Item` t1
#         INNER JOIN `tabPurchase Receipt Item` t2
#         WHERE
#             t1.name > t2.name
#             AND t1.parent = t2.parent
#             AND t1.parent = %s
#             AND t1.item_code = t2.item_code
#             AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
#             AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
#     """, (parent_name,))


# @frappe.whitelist()
# def sync_external_purchase_receipt_docs(source_doctype, names):

#     if isinstance(names, str):
#         names = json.loads(names)

#     results = []

#     for name in names:
#         try:
#             ext_pr = frappe.get_doc(source_doctype, name)

#             # Check if already synced
#             existing_pr = frappe.db.get_value(
#                 "Purchase Receipt",
#                 {"remote_id": ext_pr.name},
#                 "name"
#             )

#             if existing_pr:
#                 results.append({
#                     "name": existing_pr,
#                     "status": "exists"
#                 })
#                 continue

#             pr = frappe.new_doc("Purchase Receipt")

#             # Store remote reference
#             pr.remote_id = ext_pr.name

#             if hasattr(pr, "source_site"):
#                 pr.source_site = ext_pr.source_site

#             # -----------------------------
#             # COPY MAIN FIELDS
#             # -----------------------------
#             for field, value in ext_pr.as_dict().items():

#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "taxes", "remote_id"]
#                     and hasattr(pr, field)
#                 ):
#                     pr.set(field, value)

#             # -----------------------------
#             # ITEMS
#             # -----------------------------
#             pr.set("items", [])

#             for row in ext_pr.items:

#                 item_row = {}

#                 for field, value in row.as_dict().items():

#                     if (
#                         field not in SYSTEM_FIELDS
#                         and field not in IGNORE_ITEM_FIELDS
#                     ):
#                         item_row[field] = value

#                 if not item_row.get("warehouse"):
#                     item_row["warehouse"] = DEFAULT_WAREHOUSE

#                 pr.append("items", item_row)

#             # -----------------------------
#             # FLAGS
#             # -----------------------------
#             pr.flags.ignore_permissions = True
#             pr.flags.ignore_mandatory = True
#             pr.flags.ignore_validate = True
#             frappe.flags.ignore_stock_validation = True

#             # -----------------------------
#             # INSERT
#             # -----------------------------
#             pr.insert(
#                 ignore_permissions=True,
#                 ignore_links=True,
#                 ignore_mandatory=True
#             )

#             # ⭐ ONLY FIX (remove draft duplicates)
#             clean_duplicate_pr_items(pr.name)
#             frappe.db.commit()

#             # -----------------------------
#             # FORCE SAME NAME AS EXTERNAL
#             # -----------------------------
#             if pr.name != ext_pr.name:

#                 frappe.db.sql("""
#                     UPDATE `tabPurchase Receipt`
#                     SET name = %s
#                     WHERE name = %s
#                 """, (ext_pr.name, pr.name))

#                 for child_table in ["Purchase Receipt Item"]:
#                     frappe.db.sql("""
#                         UPDATE `tab{0}`
#                         SET parent = %s
#                         WHERE parent = %s
#                     """.format(child_table), (ext_pr.name, pr.name))

#                 frappe.db.commit()
#                 pr.name = ext_pr.name

#             # -----------------------------
#             # DOCSTATUS SYNC
#             # -----------------------------
#             if ext_pr.docstatus == 1:
#                 pr.submit()

#             elif ext_pr.docstatus == 2:
#                 pr.submit()
#                 pr.cancel()

#             results.append({
#                 "name": pr.name,
#                 "status": "synced"
#             })

#         except Exception as e:

#             error = frappe.get_traceback()

#             frappe.log_error(
#                 title=f"Purchase Receipt Sync Error: {name}",
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
    "prevdoc_doctype",
    "prevdoc_docname"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


# ⭐ ONLY ADDITION (duplicate cleanup)
def clean_duplicate_pr_items(parent_name):
    frappe.db.sql("""
        DELETE t1 FROM `tabPurchase Receipt Item` t1
        INNER JOIN `tabPurchase Receipt Item` t2
        WHERE
            t1.name > t2.name
            AND t1.parent = t2.parent
            AND t1.parent = %s
            AND t1.item_code = t2.item_code
            AND IFNULL(t1.qty, 0) = IFNULL(t2.qty, 0)
            AND IFNULL(t1.rate, 0) = IFNULL(t2.rate, 0)
    """, (parent_name,))



def resolve_local_po(remote_or_local_po, company_abbr=None):
    if not remote_or_local_po:
        return None

    # 1. First check: prefixed local name (highest priority)
    if company_abbr and not remote_or_local_po.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_po}"
        if frappe.db.exists("Purchase Order", prefixed):
            return prefixed

    # 2. Second check: remote_id lookup
    local_name = frappe.db.get_value(
        "Purchase Order", {"remote_id": remote_or_local_po}, "name"
    )
    if local_name:
        return local_name

    # 3. Last fallback: raw name if it exists
    if frappe.db.exists("Purchase Order", remote_or_local_po):
        return remote_or_local_po

    return None


def get_po_detail(purchase_order, item_code):
    if not purchase_order:
        return None
    return frappe.db.get_value(
        "Purchase Order Item",
        {"parent": purchase_order, "item_code": item_code},
        "name"
    )    

def resolve_local_mr(remote_or_local_mr, company_abbr=None):
    if not remote_or_local_mr:
        return None

    # 1. First check: prefixed local name (highest priority)
    if company_abbr and not remote_or_local_mr.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_or_local_mr}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed

    # 2. Second check: remote_id lookup
    local_name = frappe.db.get_value(
        "Material Request", {"remote_id": remote_or_local_mr}, "name"
    )
    if local_name:
        return local_name

    # 3. Last fallback: raw name if it exists
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


  


@frappe.whitelist()
def sync_external_purchase_receipt_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_pr = frappe.get_doc(source_doctype, name)
            remote_id = ext_pr.name

            # ------------------------------------------------
            # BUILD TARGET NAME: {company_abbr}-{remote_id}
            # Same pattern as Sales Order / Sales Invoice sync
            # ------------------------------------------------
            company_abbr = frappe.db.get_value('Company', ext_pr.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Purchase Receipt Sync: no company abbr found for company "
                    f"'{ext_pr.company}' on {remote_id}. Name will be created "
                    f"without a company prefix.",
                    "ExternalPurchaseReceipt Sync - missing company abbr"
                )

            target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # Detect actual remote-id fieldname on Purchase Receipt.
            # Site has it as "custom_remote_id", not "remote_id" -
            # setting the wrong one silently no-ops, so we resolve
            # it dynamically instead of hardcoding either name.
            # ------------------------------------------------
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

            # Check if already synced
            existing_pr = frappe.db.get_value(
                "Purchase Receipt",
                {remote_id_field: remote_id},
                "name"
            )

            if existing_pr:
                results.append({
                    "name": existing_pr,
                    "status": "exists"
                })
                continue

            pr = frappe.new_doc("Purchase Receipt")

            # ------------------------------------------------
            # SET NAME WITH COMPANY ABBR PREFIX
            # Same pattern as Sales Order / Sales Invoice
            # ------------------------------------------------
            pr.set(remote_id_field, remote_id)
            pr.name = target_name
            pr.flags.name_set = True

            # -----------------------------
            # FLAGS
            # -----------------------------
            pr.flags.ignore_permissions = True
            pr.flags.ignore_mandatory = True
            pr.flags.ignore_validate = True
            pr.flags.ignore_naming_series = True
            frappe.flags.ignore_stock_validation = True

            if hasattr(pr, "source_site"):
                pr.source_site = ext_pr.source_site

            # -----------------------------
            # COPY MAIN FIELDS
            # -----------------------------
            for field, value in ext_pr.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "remote_id", "custom_remote_id"]
                    and hasattr(pr, field)
                ):
                    pr.set(field, value)

            # -----------------------------
            # ITEMS
            # -----------------------------
            pr.set("items", [])

            for row in ext_pr.items:

                item_row = {}

                for field, value in row.as_dict().items():

                    if (
                        field not in SYSTEM_FIELDS
                        and field not in IGNORE_ITEM_FIELDS
                    ):
                        item_row[field] = value

                # custom_remote_id tracks the external row's own identity.
                # "name" is a SYSTEM_FIELD so it gets stripped above -
                # map it explicitly instead of relying on the generic copy.
                                # custom_remote_id tracks the external row's own identity.
                # "name" is a SYSTEM_FIELD so it gets stripped above -
                # map it explicitly instead of relying on the generic copy.
                                # custom_remote_id tracks the external row's own identity.
                # "name" is a SYSTEM_FIELD so it gets stripped above -
                # map it explicitly instead of relying on the generic copy.
                if frappe.get_meta("Purchase Receipt Item").has_field("custom_remote_id"):
                    item_row["custom_remote_id"] = row.name

                # Resolve Purchase Order with company abbreviation
                                # Resolve Purchase Order with company abbreviation
                if row.get("purchase_order"):
                    local_po = resolve_local_po(row.purchase_order, company_abbr)
                    item_row["purchase_order"] = local_po  # Will be None if not found
                    item_row["po_detail"] = get_po_detail(
                        local_po, row.item_code
                    ) if local_po else None
                else:
                    item_row["purchase_order"] = None
                    item_row["po_detail"] = None

                # Resolve Material Request with company abbreviation
                if row.get("material_request"):
                    local_mr = resolve_local_mr(row.material_request, company_abbr)
                    item_row["material_request"] = local_mr
                    item_row["material_request_item"] = get_mr_detail(
                        local_mr, row.item_code
                    ) if local_mr else None

                if not item_row.get("warehouse"):
                    item_row["warehouse"] = DEFAULT_WAREHOUSE

                pr.append("items", item_row)

            # -----------------------------
            # INSERT
            # -----------------------------
            pr.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # ⭐ ONLY FIX (remove draft duplicates)
            clean_duplicate_pr_items(pr.name)
            frappe.db.commit()

            # ------------------------------------------------
            # NAMING SAFETY NET: If Frappe renamed it, force it
            # back to target_name - but only if that name isn't
            # already taken by another doc.
            # Same pattern as Sales Order / Sales Invoice
            # ------------------------------------------------
            if pr.name != target_name:

                if not frappe.db.exists("Purchase Receipt", target_name):

                    old_name = pr.name

                    frappe.db.sql("""
                        UPDATE `tabPurchase Receipt`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, old_name))

                    for child_table in ["Purchase Receipt Item"]:
                        if frappe.db.exists("DocType", child_table):
                            frappe.db.sql("""
                                UPDATE `tab{0}`
                                SET parent = %s
                                WHERE parent = %s
                            """.format(child_table), (target_name, old_name))

                    frappe.db.commit()
                    pr.name = target_name

                else:
                    frappe.log_error(
                        f"Purchase Receipt Sync: could not rename {pr.name} to "
                        f"{target_name} because that name already exists.",
                        "ExternalPurchaseReceipt Sync - name collision"
                    )

            # -----------------------------
            # DOCSTATUS SYNC
            # -----------------------------
            if ext_pr.docstatus == 1:
                pr.submit()

            elif ext_pr.docstatus == 2:
                pr.submit()
                pr.cancel()

            results.append({
                "name": pr.name,
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

