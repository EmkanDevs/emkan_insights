import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "against_sales_order",
    "so_detail",
    "sales_order",
    "sales_order_item",
    "prevdoc_doctype",
    "prevdoc_docname"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


@frappe.whitelist()
def sync_external_delivery_note_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_dn = frappe.get_doc(source_doctype, name)

            # Check if already synced
            existing_dn = frappe.db.get_value(
                "Delivery Note",
                {"remote_id": ext_dn.name},
                "name"
            )

            if existing_dn:
                results.append({
                    "name": existing_dn,
                    "status": "exists"
                })
                continue

            dn = frappe.new_doc("Delivery Note")

            # Force same name
            # dn.name = ext_dn.name
            # dn.set("__newname", ext_dn.name)

            # Store remote reference
            dn.remote_id = ext_dn.name

            # -----------------------------
            # COPY MAIN FIELDS
            # -----------------------------
            for field, value in ext_dn.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "remote_id"]
                    and hasattr(dn, field)
                ):
                    dn.set(field, value)

            # -----------------------------
            # HANDLE RETURN DELIVERY NOTE
            # -----------------------------
            if ext_dn.get("is_return"):

                if frappe.db.exists("Delivery Note", ext_dn.return_against):
                    dn.is_return = 1
                    dn.return_against = ext_dn.return_against
                else:
                    # If original DN not present, convert to normal DN
                    dn.is_return = 0
                    dn.return_against = None

            # -----------------------------
            # ITEMS
            # -----------------------------
            dn.set("items", [])

            for row in ext_dn.items:

                item_row = {}

                for field, value in row.as_dict().items():

                    if (
                        field not in SYSTEM_FIELDS
                        and field not in IGNORE_ITEM_FIELDS
                    ):
                        item_row[field] = value

                # Ensure warehouse exists
                if not item_row.get("warehouse"):
                    item_row["warehouse"] = DEFAULT_WAREHOUSE

                dn.append("items", item_row)

            # -----------------------------
            # TAXES
            # -----------------------------
            if hasattr(ext_dn, "taxes"):

                dn.set("taxes", [])

                for row in ext_dn.taxes:

                    tax_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value

                    dn.append("taxes", tax_row)

            # -----------------------------
            # IGNORE STOCK VALIDATION
            # -----------------------------
            dn.flags.ignore_permissions = True
            dn.flags.ignore_mandatory = True
            dn.flags.ignore_validate = True
            dn.flags.ignore_validate_update_after_submit = True
            frappe.flags.ignore_stock_validation = True

            # -----------------------------
            # INSERT
            # -----------------------------
            dn.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )
              # -----------------------------
              # FORCE SAME NAME AS EXTERNAL
              # -----------------------------
            if dn.name != ext_dn.name:
                frappe.db.sql("""
                    UPDATE `tabDelivery Note`
                    SET name = %s
                    WHERE name = %s
                """, (ext_dn.name, dn.name))
                for child_table in ["Delivery Note Item", "Sales Taxes and Charges"]:
                    frappe.db.sql("""
                        UPDATE `tab{0}`
                        SET parent = %s
                        WHERE parent = %s
                """.format(child_table), (ext_dn.name, dn.name))

                frappe.db.commit()
                dn.name = ext_dn.name

            # -----------------------------
            # DOCSTATUS SYNC
            # -----------------------------
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