# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalSupplierQuotation(Document):
	pass
# class ExternalSupplierQuotation(Document):

#     def before_save(self):

#         # ⭐ Fix invalid status coming from remote
#         valid_status = ["Draft", "Submitted", "Stopped", "Cancelled"]

#         if self.status not in valid_status:
#             self.status = "Draft"

#         # ⭐ Ensure at least one item exists (very important)
#         if not self.get("items"):
#             frappe.throw("Supplier Quotation Items not synced")

#         # ⭐ Prevent invalid submitted state
#         if self.docstatus == 1 and self.status == "Draft":
#             self.status = "Submitted"

#         # ⭐ Prevent cancelled mismatch
#         if self.docstatus == 2:
#             self.status = "Cancelled"

#         # ⭐ Avoid expiry validation crash
#         if self.valid_till and self.transaction_date:
#             if self.valid_till < self.transaction_date:
                # self.valid_till = self.transaction_date