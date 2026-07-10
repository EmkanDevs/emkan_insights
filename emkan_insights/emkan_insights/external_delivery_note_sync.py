# import frappe
# import json

# SYSTEM_FIELDS = {
#     "name", "owner", "creation", "modified", "modified_by",
#     "docstatus", "idx", "doctype", "__last_sync_on",
#     "parent", "parentfield", "parenttype"
# }

# IGNORE_ITEM_FIELDS = {
#     "against_sales_order",
#     "so_detail",
#     "sales_order",
#     "sales_order_item",
#     "prevdoc_doctype",
#     "prevdoc_docname"
# }

# DEFAULT_WAREHOUSE = "Stores - IMC"


# @frappe.whitelist()
# def sync_external_delivery_note_docs(source_doctype, names):

#     if isinstance(names, str):
#         names = json.loads(names)

#     results = []

#     for name in names:
#         try:
#             ext_dn = frappe.get_doc(source_doctype, name)

#             # Check if already synced
#             existing_dn = frappe.db.get_value(
#                 "Delivery Note",
#                 {"remote_id": ext_dn.name},
#                 "name"
#             )

#             if existing_dn:
#                 results.append({
#                     "name": existing_dn,
#                     "status": "exists"
#                 })
#                 continue

#             dn = frappe.new_doc("Delivery Note")

#             # Force same name
#             # dn.name = ext_dn.name
#             # dn.set("__newname", ext_dn.name)

#             # Store remote reference
#             dn.remote_id = ext_dn.name

#             # -----------------------------
#             # COPY MAIN FIELDS
#             # -----------------------------
#             for field, value in ext_dn.as_dict().items():

#                 if (
#                     field not in SYSTEM_FIELDS
#                     and field not in ["items", "taxes", "remote_id"]
#                     and hasattr(dn, field)
#                 ):
#                     dn.set(field, value)

#             # -----------------------------
#             # HANDLE RETURN DELIVERY NOTE
#             # -----------------------------
#             if ext_dn.get("is_return"):

#                 if frappe.db.exists("Delivery Note", ext_dn.return_against):
#                     dn.is_return = 1
#                     dn.return_against = ext_dn.return_against
#                 else:
#                     # If original DN not present, convert to normal DN
#                     dn.is_return = 0
#                     dn.return_against = None

#             # -----------------------------
#             # ITEMS
#             # -----------------------------
#             dn.set("items", [])

#             for row in ext_dn.items:

#                 item_row = {}

#                 for field, value in row.as_dict().items():

#                     if (
#                         field not in SYSTEM_FIELDS
#                         and field not in IGNORE_ITEM_FIELDS
#                     ):
#                         item_row[field] = value

#                 # Ensure warehouse exists
#                 if not item_row.get("warehouse"):
#                     item_row["warehouse"] = DEFAULT_WAREHOUSE

#                 dn.append("items", item_row)

#             # -----------------------------
#             # TAXES
#             # -----------------------------
#             if hasattr(ext_dn, "taxes"):

#                 dn.set("taxes", [])

#                 for row in ext_dn.taxes:

#                     tax_row = {}

#                     for field, value in row.as_dict().items():

#                         if field not in SYSTEM_FIELDS:
#                             tax_row[field] = value

#                     dn.append("taxes", tax_row)

#             # -----------------------------
#             # IGNORE STOCK VALIDATION
#             # -----------------------------
#             dn.flags.ignore_permissions = True
#             dn.flags.ignore_mandatory = True
#             dn.flags.ignore_validate = True
#             dn.flags.ignore_validate_update_after_submit = True
#             frappe.flags.ignore_stock_validation = True

#             # -----------------------------
#             # INSERT
#             # -----------------------------
#             dn.insert(
#                 ignore_permissions=True,
#                 ignore_links=True,
#                 ignore_mandatory=True
#             )
#               # -----------------------------
#               # FORCE SAME NAME AS EXTERNAL
#               # -----------------------------
#             if dn.name != ext_dn.name:
#                 frappe.db.sql("""
#                     UPDATE `tabDelivery Note`
#                     SET name = %s
#                     WHERE name = %s
#                 """, (ext_dn.name, dn.name))
#                 for child_table in ["Delivery Note Item", "Sales Taxes and Charges"]:
#                     frappe.db.sql("""
#                         UPDATE `tab{0}`
#                         SET parent = %s
#                         WHERE parent = %s
#                 """.format(child_table), (ext_dn.name, dn.name))

#                 frappe.db.commit()
#                 dn.name = ext_dn.name

#             # -----------------------------
#             # DOCSTATUS SYNC
#             # -----------------------------
#             if ext_dn.docstatus == 1:
#                 dn.submit()

#             elif ext_dn.docstatus == 2:
#                 dn.submit()
#                 dn.cancel()

#             results.append({
#                 "name": dn.name,
#                 "status": "synced"
#             })

#         except Exception as e:

#             error = frappe.get_traceback()

#             frappe.log_error(
#                 title=f"Delivery Note Sync Error: {name}",
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

# "name" is included here (not just in SYSTEM_FIELDS) so it's explicit that
# we never copy the external row's own name into a local field.
IGNORE_ITEM_FIELDS = {
    "name"
}

# Fields resolved explicitly further down - don't let the generic field-copy
# loop blindly overwrite them with unresolved raw values from the source doc.
EXPLICITLY_HANDLED_HEADER_FIELDS = {
    "items", "taxes", "remote_id", "custom_remote_id", "customer",
    "is_return", "return_against"
}

# CONFIRMED: "External Delivery Note" reuses "Delivery Note Item" as its own
# child table (there is no separate "External Delivery Note Item" doctype).
# That means `tabDelivery Note Item` physically holds rows from BOTH:
#   - real Delivery Note documents      (parenttype = "Delivery Note")
#   - External Delivery Note documents  (parenttype = "External Delivery Note")
# distinguished only by `parent` + `parenttype`. Every raw SQL statement
# below against this table MUST filter on parenttype = 'Delivery Note'
# explicitly, or it risks touching/deleting external staging rows (or,
# in theory, colliding if a name string were ever reused across doctypes).
LOCAL_ITEM_PARENTTYPE = "Delivery Note"


# ==========================================================
# DUPLICATE CLEANUP (kept as a post-insert safety net)
# ==========================================================

def clean_duplicate_dn_items(parent_name):
    """
    Belt-and-suspenders cleanup: if two item rows still ended up identical
    in every tracked field (including custom_remote_id - meaning they trace
    back to the exact same source row), remove the extra one. The real fix
    is deduping before appending (see _dedupe_source_items below); this stays
    as a safety net in case anything slips through.

    parenttype is explicitly filtered on both sides of the self-join since
    `tabDelivery Note Item` is a shared table with External Delivery Note.
    """
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
    """
    Remove orphaned child rows (items, taxes, packed items, etc.) left behind
    when a local Delivery Note with this name was previously created and then
    deleted. Only runs when the parent Delivery Note itself is absent, so it
    can never touch the children of a live Delivery Note. Always scoped to
    parenttype = 'Delivery Note' because these child tables are physically
    shared with External Delivery Note.
    """
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
    """
    Prevent duplicate rows from ever being appended in the first place,
    instead of relying only on post-insert cleanup. If the source pull
    (pagination overlap, retry, etc.) handed back the same underlying row
    more than once - identified by its own unique row name - keep only the
    first occurrence.

    Note: ext_items here is already correctly scoped to this specific
    External Delivery Note by Frappe's ORM (get_doc loads children filtered
    by parent + parenttype + parentfield), so no extra scoping is needed
    here even though the table is shared.
    """
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
# LINK RESOLVERS (same 3-tier pattern as the Sales Invoice sync)
# ==========================================================

def resolve_local_doc(doctype, remote_or_local_name, company_abbr=None):
    """
    Three-tier resolution:
    1. Prefixed local name: {company_abbr}-{remote_name}
    2. Remote ID lookup: {remote_id_field: remote_name}
    3. Raw name existence check

    Tier 3 covers the common case here: Sales Order / Delivery Note /
    Purchase Order names on the source side already come out looking like
    real local names (e.g. "SAL-ORD-IMC-26-01315"), so this resolves on
    the first raw existence check without ever needing tier 1 or 2.
    """
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
    """
    Resolve the LOCAL child-row name for a link like `so_detail`.

    PRIMARY: match on the source row's own remote child id via
    `custom_remote_id`. This is exact and unambiguous - it maps to the precise
    Sales Order line the source pointed at, even when the same item_code
    appears on several lines of that order.

    FALLBACK: match by (parent, item_code) only when the remote id can't be
    resolved (e.g. the referenced Sales Order line was never synced locally).
    Matching by item_code alone is what previously produced WRONG so_detail
    mappings on orders that repeat an item.

    `parenttype` is filtered explicitly because child tables such as
    "Sales Order Item" are physically shared between real Sales Orders and
    External Sales Orders.
    """
    if parenttype is None:
        # e.g. "Sales Order Item" -> "Sales Order"
        parenttype = doctype[:-5] if doctype.endswith(" Item") else None

    if remote_child_id:
        # Exact: the remote child row under the specific resolved parent.
        if parent:
            filters = {"custom_remote_id": remote_child_id, "parent": parent}
            if parenttype:
                filters["parenttype"] = parenttype
            match = frappe.db.get_value(doctype, filters, "name")
            if match:
                return match

        # The remote child may exist under several rows sharing the same
        # custom_remote_id - typically ONE live row plus ORPHANED rows
        # (parent NULL/empty, or pointing at a parent doc that no longer
        # exists) left behind by earlier sync runs. Never return an orphan:
        # pick the row whose parent doc actually exists.
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
    """
    Resolve the customer explicitly with a log (not a hard failure) if it
    can't be resolved - leave blank and let ignore_mandatory cover it,
    rather than dropping the whole delivery note.
    """
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
    """Resolve a real warehouse for the delivery note's own company."""
    if not company:
        return None
    return frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")


def _ensure_item_uom(item_code, uom):
    """
    ERPNext throws 'UOM {uom} not found in Item {item_code}' if a transaction
    row uses a UOM that isn't registered on that Item. Auto-register it
    (conversion factor 1.0) instead of letting the whole delivery note fail.
    """
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
        frappe.db.commit()
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

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_dn = frappe.get_doc(source_doctype, name)
            remote_id = ext_dn.name

            # ------------------------------------------------
            # BUILD TARGET NAME: {company_abbr}-{remote_id}
            # ------------------------------------------------
            company_abbr = frappe.db.get_value("Company", ext_dn.company, "abbr") or ""

            if not company_abbr:
                frappe.log_error(
                    f"Delivery Note Sync: no company abbr found for company "
                    f"'{ext_dn.company}' on {remote_id}. Name will be created "
                    f"without a company prefix.",
                    "ExternalDeliveryNote Sync - missing company abbr"
                )

            # Don't double-prefix if the source already stamped the abbr on
            # (would otherwise produce names like "IMC-IMC-DN-25-00123").
            if company_abbr and remote_id.startswith(f"{company_abbr}-"):
                target_name = remote_id
            else:
                target_name = f"{company_abbr}-{remote_id}" if company_abbr else remote_id

            # ------------------------------------------------
            # Dynamic remote_id field detection
            # ------------------------------------------------
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

            # ------------------------------------------------
            # Check existing by dynamic remote_id field
            # ------------------------------------------------
            existing_dn = frappe.db.get_value(
                "Delivery Note",
                {remote_id_field: remote_id},
                "name"
            )

            if existing_dn:
                results.append({
                    "name": existing_dn,
                    "status": "exists"
                })
                continue

            dn = frappe.new_doc("Delivery Note")

            # ------------------------------------------------
            # SET NAME WITH COMPANY ABBR PREFIX
            # ------------------------------------------------
            dn.set(remote_id_field, remote_id)
            dn.name = target_name
            dn.flags.name_set = True

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            dn.flags.ignore_permissions = True
            dn.flags.ignore_mandatory = True
            dn.flags.ignore_validate = True
            dn.flags.ignore_validate_update_after_submit = True
            dn.flags.ignore_links = True
            dn.flags.ignore_naming_series = True
            frappe.flags.ignore_stock_validation = True

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # ------------------------------------------------
            for field, value in ext_dn.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in EXPLICITLY_HANDLED_HEADER_FIELDS
                    and hasattr(dn, field)
                ):
                    dn.set(field, value)

            # Resolve customer explicitly instead of copying it verbatim
            # through the generic loop above.
            dn.customer = _resolve_customer(ext_dn.get("customer"), company_abbr)

            # ------------------------------------------------
            # HANDLE RETURN DELIVERY NOTE
            # ------------------------------------------------
            if ext_dn.get("is_return"):
                local_return_against = resolve_local_doc(
                    "Delivery Note", ext_dn.get("return_against"), company_abbr
                ) if ext_dn.get("return_against") else None

                if local_return_against:
                    dn.is_return = 1
                    dn.return_against = local_return_against
                else:
                    # If the original DN isn't present locally, fall back to
                    # a normal DN rather than failing the whole sync.
                    dn.is_return = 0
                    dn.return_against = None

            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
            dn.set("items", [])

            # Dedupe by source row identity before ever appending, instead
            # of only cleaning up duplicates after insert.
            deduped_items = _dedupe_source_items(ext_dn.items)

            for row in deduped_items:

                item_row = {}

                for field, value in row.as_dict().items():

                    if (
                        field not in SYSTEM_FIELDS
                        and field not in IGNORE_ITEM_FIELDS
                    ):
                        item_row[field] = value

                # Resolve the Sales Order link (3-tier, company-abbr aware).
                # Source-side names here already come out looking like real
                # local names (e.g. "SAL-ORD-IMC-26-01315"), so this usually
                # resolves on the raw-existence tier - but if the referenced
                # Sales Order was never synced locally (or was cleaned up
                # from the source before sync), this correctly resolves to
                # None rather than failing the whole delivery note.
                so_ref = row.get("against_sales_order") or row.get("sales_order")
                local_so = resolve_local_doc("Sales Order", so_ref, company_abbr) if so_ref else None

                # Map the exact Sales Order line using the source row's own
                # remote `so_detail` id (mapped via custom_remote_id), falling
                # back to (SO, item_code) only when that can't be resolved.
                local_so_detail = get_child_detail(
                    "Sales Order Item",
                    local_so,
                    row.item_code,
                    remote_child_id=row.get("so_detail"),
                    parenttype="Sales Order",
                )

                # If the Sales Order link itself wasn't resolvable but we still
                # matched the exact SO line via its remote id, backfill the
                # parent Sales Order from that line so the two stay consistent.
                if not local_so and local_so_detail:
                    local_so = frappe.db.get_value(
                        "Sales Order Item", local_so_detail, "parent"
                    )

                item_row["against_sales_order"] = local_so
                item_row["so_detail"] = local_so_detail

                # These raw source fields don't exist / aren't meaningful on
                # a local Delivery Note Item - drop them now that
                # against_sales_order / so_detail have been resolved above.
                item_row.pop("sales_order", None)
                item_row.pop("sales_order_item", None)
                item_row.pop("prevdoc_doctype", None)
                item_row.pop("prevdoc_docname", None)

                # Map external row identity to custom_remote_id
                if frappe.get_meta("Delivery Note Item").has_field("custom_remote_id"):
                    item_row["custom_remote_id"] = row.name

                # Resolve a real per-company default warehouse.
                if not item_row.get("warehouse"):
                    item_row["warehouse"] = _resolve_default_warehouse(ext_dn.company)

                # Auto-register the item's UOM if missing - avoids
                # "UOM X not found in Item Y".
                if item_row.get("item_code") and item_row.get("uom"):
                    _ensure_item_uom(item_row["item_code"], item_row["uom"])

                dn.append("items", item_row)

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_dn, "taxes"):

                dn.set("taxes", [])

                for row in ext_dn.taxes:

                    tax_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value

                    dn.append("taxes", tax_row)

            # ------------------------------------------------
            # PURGE STALE / ORPHANED CHILD ROWS FOR THIS TARGET NAME
            # ------------------------------------------------
            # If a Delivery Note with this name was previously created and its
            # parent row later removed, the child rows (items/taxes/...) can be
            # left behind as orphans. Because the child tables are keyed only by
            # (parent, parenttype), those orphans would stack on top of the rows
            # we insert now and show up as duplicate items/taxes. Clear them
            # first. Always parenttype-scoped so this can only ever touch LOCAL
            # Delivery Note rows, never the shared External Delivery Note rows.
            _purge_orphan_local_dn_children(target_name)

            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            dn.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # Safety net - should be a no-op now that items are deduped
            # before appending, but kept in case anything slips through.
            # Explicitly parenttype-scoped since the item table is shared
            # with External Delivery Note.
            clean_duplicate_dn_items(dn.name)
            frappe.db.commit()

            # ------------------------------------------------
            # NAMING SAFETY NET
            # ------------------------------------------------
            if dn.name != target_name:

                if not frappe.db.exists("Delivery Note", target_name):

                    old_name = dn.name

                    frappe.db.sql("""
                        UPDATE `tabDelivery Note`
                        SET name = %s
                        WHERE name = %s
                    """, (target_name, old_name))

                    # `tabDelivery Note Item` is shared with External
                    # Delivery Note - always scope by parenttype so this
                    # can only ever touch this Delivery Note's own rows.
                    frappe.db.sql("""
                        UPDATE `tabDelivery Note Item`
                        SET parent = %s
                        WHERE parent = %s AND parenttype = %s
                    """, (target_name, old_name, LOCAL_ITEM_PARENTTYPE))

                    if frappe.db.exists("DocType", "Sales Taxes and Charges"):
                        frappe.db.sql("""
                            UPDATE `tabSales Taxes and Charges`
                            SET parent = %s
                            WHERE parent = %s AND parenttype = %s
                        """, (target_name, old_name, LOCAL_ITEM_PARENTTYPE))

                    frappe.db.commit()
                    dn.name = target_name

                else:
                    frappe.log_error(
                        f"Delivery Note Sync: could not rename {dn.name} to "
                        f"{target_name} because that name already exists.",
                        "ExternalDeliveryNote Sync - name collision"
                    )

            # ------------------------------------------------
            # DOCSTATUS SYNC
            # ------------------------------------------------
            if ext_dn.docstatus == 1:
                dn.submit()

            elif ext_dn.docstatus == 2:
                dn.submit()
                dn.cancel()

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