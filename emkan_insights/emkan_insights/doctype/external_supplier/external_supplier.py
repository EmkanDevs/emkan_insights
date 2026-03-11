# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ExternalSupplier(Document):
	pass

# @frappe.whitelist()
# def sync_external_supplier_to_supplier(external_supplier_name):
#     """
#     Create or update Supplier from External Supplier
#     Supplier.name = External Supplier.remote_id
#     """

#     ext = frappe.get_doc("External Supplier", external_supplier_name)

#     if not ext.remote_id:
#         frappe.throw("Remote ID is mandatory to sync Supplier")

#     if frappe.db.exists("Supplier", ext.remote_id):
#         supplier_doc = frappe.get_doc("Supplier", ext.remote_id)
#         is_new = False
#     else:
#         supplier_doc = frappe.new_doc("Supplier")
#         supplier_doc.set_new_name(ext.remote_id)  # ✅ primary key
#         is_new = True

#     supplier_fields = {
#         df.fieldname
#         for df in frappe.get_meta("Supplier").fields
#     }

#     ignore_fields = {
#         "name", "owner", "creation", "modified", "modified_by",
#         "docstatus", "idx", "__last_sync_on"
#     }

#     for fieldname, value in ext.as_dict().items():
#         if fieldname in supplier_fields and fieldname not in ignore_fields:
#             supplier_doc.set(fieldname, value)

#     supplier_doc.name = ext.remote_id
#     supplier_doc.source_site = ext.source_site

#     supplier_doc.save(ignore_permissions=True)

#     return {
#         "status": "success",
#         "supplier": supplier_doc.name,
#         "supplier_name": supplier_doc.supplier_name,
#         "action": "created" if is_new else "updated"
#     }

from typing import List, Union


@frappe.whitelist()
def sync_external_suppliers(
    external_suppliers: Union[str, List[str], None] = None,
    names: Union[str, List[str], None] = None,
    source_doctype: str | None = None,
):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    # Accept both payload formats:
    # 1) external_suppliers=[...]
    # 2) names=[...] (used by list actions)
    supplier_names = external_suppliers or names
    return sync_external_docs(source_doctype="External Supplier", names=supplier_names)




@frappe.whitelist()
def sync_external_records(names):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(source_doctype="External Supplier", names=names)
