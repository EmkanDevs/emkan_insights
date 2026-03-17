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
def sync_external_sales_order_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_so = frappe.get_doc(source_doctype, name)

            existing_so = frappe.db.get_value(
                "Sales Order",
                {"remote_id": ext_so.name},
                "name"
            )

            if existing_so:
                results.append({
                    "name": existing_so,
                    "status": "exists"
                })
                continue

            so = frappe.new_doc("Sales Order")

            # FORCE SAME NAME
            so.name = ext_so.name
            so.set("__newname", ext_so.name)

            so.remote_id = ext_so.name

            # COPY MAIN FIELDS
            for field, value in ext_so.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "taxes", "remote_id"]
                    and hasattr(so, field)
                ):
                    so.set(field, value)

            # ITEMS
            so.set("items", [])

            for row in ext_so.items:

                item_row = {}

                for field, value in row.as_dict().items():

                    if (
                        field not in SYSTEM_FIELDS
                        and field not in IGNORE_ITEM_FIELDS
                    ):
                        item_row[field] = value

                if not item_row.get("warehouse"):
                    item_row["warehouse"] = DEFAULT_WAREHOUSE

                so.append("items", item_row)

            # TAXES
            if hasattr(ext_so, "taxes"):

                so.set("taxes", [])

                for row in ext_so.taxes:

                    tax_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            tax_row[field] = value

                    so.append("taxes", tax_row)

            # INSERT
            so.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # 🔥 SYNC DOCSTATUS
            if ext_so.docstatus == 1:
                so.submit()

            elif ext_so.docstatus == 2:
                so.submit()
                so.cancel()

            results.append({
                "name": so.name,
                "status": "synced"
            })

        except Exception:

            frappe.log_error(
                f"Sales Order Sync Error: {name}",
                frappe.get_traceback()
            )

            results.append({
                "name": name,
                "status": "failed"
            })

    return results