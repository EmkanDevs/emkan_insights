import frappe
from frappe.model.document import Document
import json


class ExternalWorkOrder(Document):
    pass

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
def sync_external_work_order_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    results = []

    for name in names:
        try:
            ext_wo = frappe.get_doc(source_doctype, name)

            # ------------------------------------------------
            # CHECK EXISTING
            # ------------------------------------------------
            existing_wo = frappe.db.get_value(
                "Work Order",
                {"remote_id": ext_wo.remote_id},
                "name"
            )

            if existing_wo:
                results.append({
                    "name": existing_wo,
                    "status": "exists"
                })
                continue

            wo = frappe.new_doc("Work Order")

            # FIX: Use remote_id as the target name
            target_name = ext_wo.remote_id
            wo.remote_id = target_name

            if hasattr(wo, "source_site"):
                wo.source_site = ext_wo.source_site

            # ------------------------------------------------
            # COPY MAIN FIELDS
            # ------------------------------------------------
            for field, value in ext_wo.as_dict().items():

                if (
                    field not in SYSTEM_FIELDS
                    and field not in ["items", "operations", "scrap_items", "required_items", "remote_id"]
                    and hasattr(wo, field)
                ):
                    wo.set(field, value)

            # ------------------------------------------------
            # REQUIRED ITEMS (Raw Materials for Work Order)
            # ------------------------------------------------
            if hasattr(ext_wo, "required_items"):

                wo.set("required_items", [])

                for row in ext_wo.required_items:

                    item_row = {}

                    for field, value in row.as_dict().items():

                        if (
                            field not in SYSTEM_FIELDS
                            and field not in IGNORE_ITEM_FIELDS
                        ):
                            item_row[field] = value

                    # Remove invalid BOM links
                    if item_row.get("bom_no") and not frappe.db.exists("BOM", item_row["bom_no"]):
                        item_row["bom_no"] = None

                    wo.append("required_items", item_row)

            # ------------------------------------------------
            # OPERATIONS
            # ------------------------------------------------
            if hasattr(ext_wo, "operations"):

                wo.set("operations", [])

                for row in ext_wo.operations:

                    op_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            op_row[field] = value

                    # Remove invalid Workstation links
                    if op_row.get("workstation") and not frappe.db.exists("Workstation", op_row["workstation"]):
                        op_row["workstation"] = None

                    # Remove invalid Workstation Type links
                    if op_row.get("workstation_type") and not frappe.db.exists("Workstation Type", op_row["workstation_type"]):
                        op_row["workstation_type"] = None

                    # Remove invalid BOM links
                    if op_row.get("bom") and not frappe.db.exists("BOM", op_row["bom"]):
                        op_row["bom"] = None

                    wo.append("operations", op_row)

            # ------------------------------------------------
            # SCRAP ITEMS — FIXED: Only append if table exists
            # ------------------------------------------------
            # NOTE: Standard ERPNext does NOT have a separate scrap_items
            # child table on Work Order. Scrap is handled in BOM or 
            # via required_items. Only process if the doctype actually exists.
            if hasattr(ext_wo, "scrap_items") and frappe.db.exists("DocType", "Work Order Scrap Item"):
                wo.set("scrap_items", [])

                for row in ext_wo.scrap_items:

                    scrap_row = {}

                    for field, value in row.as_dict().items():

                        if field not in SYSTEM_FIELDS:
                            scrap_row[field] = value

                    wo.append("scrap_items", scrap_row)

            # ------------------------------------------------
            # FLAGS
            # ------------------------------------------------
            wo.flags.ignore_permissions = True
            wo.flags.ignore_validate = True
            wo.flags.ignore_mandatory = True
            wo.flags.ignore_links = True

            # ------------------------------------------------
            # INSERT
            # ------------------------------------------------
            wo.insert(
                ignore_permissions=True,
                ignore_links=True,
                ignore_mandatory=True
            )

            # ------------------------------------------------
            # FORCE SAME NAME AS REMOTE ID
            # ------------------------------------------------
            if wo.name != target_name:

                frappe.db.sql("""
                    UPDATE `tabWork Order`
                    SET name = %s
                    WHERE name = %s
                """, (target_name, wo.name))

                # Update child tables — ONLY ones that actually exist
                child_tables = [
                    "Work Order Item",       # required_items
                    "Work Order Operation",  # operations
                ]
                
                # Only add scrap table if it exists in this ERPNext instance
                if frappe.db.exists("DocType", "Work Order Scrap Item"):
                    child_tables.append("Work Order Scrap Item")

                for child_table in child_tables:
                    if frappe.db.exists("DocType", child_table.replace(" ", "")):
                        frappe.db.sql("""
                            UPDATE `tab{0}`
                            SET parent = %s
                            WHERE parent = %s
                        """.format(child_table), (target_name, wo.name))

            # ------------------------------------------------
            # DOCSTATUS SYNC — FIXED: Map Status to docstatus
            # ------------------------------------------------
            # CRITICAL FIX: The CSV has 'Status' not 'docstatus'.
            # Map workflow Status to document docstatus for submit/cancel logic.
            wo_status = ext_wo.get("status") or "Draft"

            # Map External Work Order Status to target docstatus
            if wo_status in ("Completed", "In Process", "Not Started"):
                target_docstatus = 1  # Submit these
            elif wo_status == "Cancelled":
                target_docstatus = 2  # Cancel
            else:
                target_docstatus = 0  # Draft

            # Always get a completely fresh document from DB
            wo = frappe.get_doc("Work Order", target_name)

            # Re-apply all flags on fresh document
            wo.flags.ignore_permissions = True
            wo.flags.ignore_validate = True
            wo.flags.ignore_mandatory = True
            wo.flags.ignore_links = True

            # ------------------------------------------------
            # SUBMIT / CANCEL based on mapped docstatus
            # ------------------------------------------------
            try:
                if target_docstatus == 1:
                    wo.submit()

                elif target_docstatus == 2:
                    wo.submit()
                    
                    # Get fresh doc after submit, apply ALL flags, then cancel
                    wo = frappe.get_doc("Work Order", target_name)
                    wo.flags.ignore_permissions = True
                    wo.flags.ignore_validate = True
                    wo.flags.ignore_mandatory = True
                    wo.flags.ignore_links = True
                    wo.cancel()

            except Exception as submit_err:
                frappe.log_error(
                    f"Work Order Submit/Cancel Error for {target_name}: {str(submit_err)}",
                    "ExternalWorkOrder Sync - Submit/Cancel"
                )
                results.append({
                    "name": target_name,
                    "status": "synced_draft",
                    "error": f"Inserted but submit/cancel failed: {str(submit_err)}"
                })
                continue

            results.append({
                "name": target_name,
                "status": "synced"
            })

        except Exception:

            frappe.log_error(
                f"Work Order Sync Error: {name}",
                frappe.get_traceback()
            )

            results.append({
                "name": name,
                "status": "failed"
            })

    return results