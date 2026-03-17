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


@frappe.whitelist()
def sync_external_purchase_receipt_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_pr = frappe.get_doc(source_doctype, name)

            # Check if already synced
            existing_pr = frappe.db.get_value(
                "Purchase Receipt",
                {"remote_id": ext_pr.name},
                "name"
            )

            if existing_pr:
                results.append({
                    "name": existing_pr,
                    "status": "exists"
                })
                continue

            pr = frappe.new_doc("Purchase Receipt")

            # Store remote reference
            pr.remote_id = ext_pr.name

            if hasattr(pr, "source_site"):
                pr.source_site = ext_pr.source_site

            # -----------------------------
            # COPY MAIN FIELDS
            # -----------------------------
            for field, value in ext_pr.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "remote_id"]
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

                if not item_row.get("warehouse"):
                    item_row["warehouse"] = DEFAULT_WAREHOUSE

                pr.append("items", item_row)

            # -----------------------------
            # FLAGS
            # -----------------------------
            pr.flags.ignore_permissions = True
            pr.flags.ignore_mandatory = True
            pr.flags.ignore_validate = True
            frappe.flags.ignore_stock_validation = True

            # -----------------------------
            # INSERT
            # -----------------------------
            pr.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # -----------------------------
            # FORCE SAME NAME AS EXTERNAL
            # -----------------------------
            if pr.name != ext_pr.name:

                frappe.db.sql("""
                    UPDATE `tabPurchase Receipt`
                    SET name = %s
                    WHERE name = %s
                """, (ext_pr.name, pr.name))

                for child_table in ["Purchase Receipt Item"]:
                    frappe.db.sql("""
                        UPDATE `tab{0}`
                        SET parent = %s
                        WHERE parent = %s
                    """.format(child_table), (ext_pr.name, pr.name))

                frappe.db.commit()
                pr.name = ext_pr.name

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