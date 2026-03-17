import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}

IGNORE_ITEM_FIELDS = {
    "quotation",
    "quotation_item",
    "prevdoc_doctype",
    "prevdoc_docname",
    "so_detail",
    "against_sales_order"
}

DEFAULT_WAREHOUSE = "Stores - IMC"


@frappe.whitelist()
def sync_external_sales_invoice_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_si = frappe.get_doc(source_doctype, name)

            # ------------------------------------------------
            # CHECK EXISTING
            # ------------------------------------------------
            existing_si = frappe.db.get_value(
                "Sales Invoice",
                {"remote_id": ext_si.name},
                "name"
            )

            if existing_si:
                results.append({
                    "name": existing_si,
                    "status": "exists"
                })
                continue

            si = frappe.new_doc("Sales Invoice")

            si.remote_id = ext_si.name

            if hasattr(si, "source_site"):
                si.source_site = ext_si.source_site

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # ------------------------------------------------
            for field, value in ext_si.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "remote_id"]
                    and hasattr(si, field)
                ):
                    si.set(field, value)

            # ------------------------------------------------
            # ITEMS
            # ------------------------------------------------
    
                si.set("items", [])

                for row in ext_si.items:

                    item_row = {}

                    for field, value in row.as_dict().items():

                        if (
                            field not in SYSTEM_FIELDS
                            and field not in IGNORE_ITEM_FIELDS
                        ):
                            item_row[field] = value

                    # Remove invalid Sales Order link
                    if row.get("sales_order") and not frappe.db.exists("Sales Order", row.sales_order):
                        item_row["sales_order"] = None
                        item_row["so_detail"] = None

                    # Remove invalid Delivery Note link
                    if row.get("delivery_note") and not frappe.db.exists("Delivery Note", row.delivery_note):
                        item_row["delivery_note"] = None
                        item_row["dn_detail"] = None

                    if not item_row.get("warehouse"):
                        item_row["warehouse"] = DEFAULT_WAREHOUSE

                    si.append("items", item_row)

            # ------------------------------------------------
            # TAXES
            # ------------------------------------------------
            if hasattr(ext_si, "taxes"):

                si.set("taxes", [])

                for row in ext_si.taxes:

                    tax_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value

                    si.append("taxes", tax_row)

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            si.flags.ignore_permissions = True
            si.flags.ignore_validate = True
            si.flags.ignore_mandatory = True

            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            si.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # ------------------------------------------------
            # FORCE SAME NAME AS EXTERNAL
            # ------------------------------------------------
            if si.name != ext_si.name:

                frappe.db.sql("""
                    UPDATE `tabSales Invoice`
                    SET name = %s
                    WHERE name = %s
                """, (ext_si.name, si.name))

                for child_table in [
                    "Sales Invoice Item",
                    "Sales Taxes and Charges",
                    "Payment Schedule"
                ]:
                    frappe.db.sql("""
                        UPDATE `tab{0}`
                        SET parent = %s
                        WHERE parent = %s
                    """.format(child_table), (ext_si.name, si.name))

                frappe.db.commit()

                si.name = ext_si.name

            # ------------------------------------------------
            # DOCSTATUS SYNC
            # ------------------------------------------------
            if ext_si.docstatus == 1:
                si.submit()

            elif ext_si.docstatus == 2:
                si.submit()
                si.cancel()

            results.append({
                "name": si.name,
                "status": "synced"
            })

        except Exception:

            frappe.log_error(
                f"Sales Invoice Sync Error: {name}",
                frappe.get_traceback()
            )

            results.append({
                "name": name,
                "status": "failed"
            })

    return results