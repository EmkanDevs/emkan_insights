import frappe
from frappe.utils import cint

from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt
from erpnext.controllers.buying_controller import BuyingController


class CustomPurchaseReceipt(PurchaseReceipt):

    def validate_with_previous_doc(self):
        BuyingController.validate_with_previous_doc(
            self,
            {
                "Purchase Order": {
                    "ref_dn_field": "purchase_order",
                    "compare_fields": [
                        ["supplier", "="],
                        ["company", "="],
                        ["currency", "="],
                    ],
                },
                "Purchase Order Item": {
                    "ref_dn_field": "purchase_order_item",
                    "compare_fields": [
                        # Removed Project validation
                        ["uom", "="],
                        ["item_code", "="],
                    ],
                    "is_child_table": True,
                    "allow_duplicate_prev_row_id": True,
                },
            },
        )

        if (
            cint(frappe.db.get_single_value("Buying Settings", "maintain_same_rate"))
            and not self.is_return
            and not self.is_internal_supplier
        ):
            self.validate_rate_with_reference_doc(
                [["Purchase Order", "purchase_order", "purchase_order_item"]]
            )