from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from erpnext.controllers.status_updater import OverAllowanceError

from emkan_insights.emkan_insights.external_pr_sync import (
    _clear_invalid_company_address,
    _has_pending_cancelled_return,
    _order_receipts_for_sync,
    _resolve_effective_external_return_parent,
    _resolve_external_purchase_receipt,
    _sync_amendment_parent,
    _sync_return_parent,
    _submit_pr_with_overflow_handling,
)


MODULE = "emkan_insights.emkan_insights.external_pr_sync"


class TestExternalPurchaseReceiptSync(FrappeTestCase):
    @patch(f"{MODULE}.frappe.db.exists", return_value=False)
    def test_clears_billing_address_not_linked_to_company(self, exists):
        pr = frappe._dict(
            doctype="Purchase Receipt",
            name="PR-0001",
            company="Construction Pillars Company",
            billing_address="No Label",
            shipping_address=None,
        )
        pr.get = lambda fieldname: pr[fieldname]
        pr.set = lambda fieldname, value: pr.update({fieldname: value})

        _clear_invalid_company_address(pr)

        self.assertIsNone(pr.billing_address)
        exists.assert_called_once_with(
            "Dynamic Link",
            {
                "parent": "No Label",
                "parenttype": "Address",
                "link_doctype": "Company",
                "link_name": "Construction Pillars Company",
            },
        )

    @patch(f"{MODULE}.frappe.get_all")
    def test_orders_all_normal_receipts_before_returns(self, get_all):
        get_all.return_value = [
            frappe._dict(
                name="RETURN-1",
                is_return=1,
                return_against="RECEIPT-1",
            ),
            frappe._dict(
                name="RETURN-2",
                is_return=0,
                return_against="RECEIPT-2",
            ),
        ]

        result = _order_receipts_for_sync(
            ["RETURN-1", "RECEIPT-1", "RETURN-2", "RECEIPT-2"]
        )

        self.assertEqual(
            result,
            ["RECEIPT-1", "RECEIPT-2", "RETURN-1", "RETURN-2"],
        )

    @patch(f"{MODULE}.frappe.db.exists", return_value="RETURN-1")
    def test_detects_pending_cancelled_return(self, exists):
        self.assertTrue(_has_pending_cancelled_return("RECEIPT-1"))
        exists.assert_called_once_with(
            "External Purchase Receipt",
            {
                "return_against": "RECEIPT-1",
                "docstatus": 2,
                "is_synced": 0,
            },
        )

    def test_cancelled_return_parent_is_submitted_before_return(self):
        ext_return = frappe._dict(
            name="RETURN-1",
            return_against="RECEIPT-1",
        )

        with (
            patch(
                f"{MODULE}.resolve_local_pr_for_item",
                side_effect=[None, "RECEIPT-1"],
            ),
            patch(
                f"{MODULE}._resolve_external_purchase_receipt",
                return_value="RECEIPT-1",
            ),
            patch(
                f"{MODULE}._resolve_effective_external_return_parent",
                return_value="RECEIPT-1",
            ),
            patch(f"{MODULE}._sync_bulk_purchase_receipts") as sync_receipts,
            patch(
                f"{MODULE}._return_parent_should_be_cancelled_after",
                return_value=True,
            ),
        ):
            result = _sync_return_parent(ext_return, "CPC", True, ())

        self.assertEqual(result, ("RECEIPT-1", True))
        sync_receipts.assert_called_once_with(
            ["RECEIPT-1"],
            strict=True,
            dependency_stack=("RETURN-1",),
            _defer_cancellation=True,
        )

    @patch(f"{MODULE}.frappe.get_all")
    def test_return_uses_matching_active_amendment(self, get_all):
        get_all.return_value = [
            frappe._dict(
                name="RECEIPT-1-1",
                docstatus=1,
                supplier="SUP-2",
                company="Company",
                currency="SAR",
            )
        ]
        ext_return = frappe._dict(
            supplier="SUP-2",
            company="Company",
            currency="SAR",
        )

        result = _resolve_effective_external_return_parent(
            ext_return, "RECEIPT-1"
        )

        self.assertEqual(result, "RECEIPT-1-1")
        get_all.assert_called_once_with(
            "External Purchase Receipt",
            filters={"amended_from": "RECEIPT-1"},
            fields=["name", "docstatus", "supplier", "company", "currency"],
            order_by="creation desc",
        )

    def test_syncs_and_cancels_amendment_parent_through_core_sync(self):
        ext_amendment = frappe._dict(
            name="RECEIPT-1-1",
            amended_from="RECEIPT-1",
        )

        with (
            patch(
                f"{MODULE}.resolve_amendment_parent",
                side_effect=[None, "RECEIPT-1"],
            ),
            patch(
                f"{MODULE}._resolve_external_purchase_receipt",
                return_value="RECEIPT-1",
            ),
            patch(f"{MODULE}._sync_bulk_purchase_receipts") as sync_receipts,
        ):
            result = _sync_amendment_parent(
                ext_amendment, "CPC", True, ()
            )

        self.assertEqual(result, "RECEIPT-1")
        sync_receipts.assert_called_once_with(
            ["RECEIPT-1"],
            strict=True,
            dependency_stack=("RECEIPT-1-1",),
            _force_cancellation=True,
        )

    def test_submit_purchase_receipt_through_orm(self):
        doc = MagicMock()

        with (
            patch(f"{MODULE}.frappe.get_doc", return_value=doc),
            patch(f"{MODULE}._apply_sync_flags"),
            patch(f"{MODULE}._ensure_local_warehouse_stock_accounts"),
            patch(f"{MODULE}._ensure_local_asset_category_accounts"),
            patch(f"{MODULE}.temporarily_enable_transaction_items"),
            patch(f"{MODULE}.frappe.db.savepoint"),
            patch(f"{MODULE}.frappe.db.rollback") as rollback,
        ):
            result = _submit_pr_with_overflow_handling("PR-0001")

        self.assertIs(result, doc)
        doc.submit.assert_called_once_with()
        rollback.assert_not_called()

    def test_over_allowance_rolls_back_before_retrying_submit(self):
        first_doc = MagicMock()
        first_doc.submit.side_effect = OverAllowanceError("Quantity exceeded")

        retry_item = MagicMock()
        retry_item.material_request = "MR-0001"
        retry_item.material_request_item = "MRI-0001"
        retry_doc = MagicMock(items=[retry_item])

        with (
            patch(
                f"{MODULE}.frappe.get_doc",
                side_effect=[first_doc, retry_doc],
            ),
            patch(f"{MODULE}._apply_sync_flags"),
            patch(f"{MODULE}._ensure_local_warehouse_stock_accounts"),
            patch(f"{MODULE}._ensure_local_asset_category_accounts"),
            patch(f"{MODULE}.temporarily_enable_transaction_items"),
            patch(f"{MODULE}.frappe.generate_hash", return_value="submit_test"),
            patch(f"{MODULE}.frappe.db.savepoint"),
            patch(f"{MODULE}.frappe.db.rollback") as rollback,
            patch(f"{MODULE}.frappe.log_error"),
        ):
            result = _submit_pr_with_overflow_handling("PR-0001")

        self.assertIs(result, retry_doc)
        rollback.assert_called_once_with(save_point="pr_submit_submit_test")
        self.assertIsNone(retry_item.material_request)
        self.assertIsNone(retry_item.material_request_item)
        retry_doc.save.assert_called_once_with(ignore_permissions=True)
        retry_doc.submit.assert_called_once_with()

    @patch("emkan_insights.emkan_insights.external_pr_sync.frappe.db.exists")
    def test_return_parent_lookup_uses_target_company_abbr(self, exists):
        exists.return_value = True

        result = _resolve_external_purchase_receipt(
            "EHC-MAT-PRE-2026-00879",
            company_abbr="CPC",
        )

        self.assertEqual(result, "EHC-MAT-PRE-2026-00879")
        self.assertEqual(
            exists.call_args_list,
            [
                call(
                    "External Purchase Receipt",
                    "EHC-MAT-PRE-2026-00879",
                ),
            ],
        )

    @patch("emkan_insights.emkan_insights.external_pr_sync.frappe.db.exists")
    def test_return_parent_lookup_does_not_duplicate_company_abbr(self, exists):
        exists.return_value = True

        result = _resolve_external_purchase_receipt(
            "CPC-MAT-PRE-2026-00879",
            company_abbr="CPC",
        )

        self.assertEqual(result, "CPC-MAT-PRE-2026-00879")
        exists.assert_called_once_with(
            "External Purchase Receipt",
            "CPC-MAT-PRE-2026-00879",
        )
