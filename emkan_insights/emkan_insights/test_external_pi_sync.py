from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_pi_sync import (
    _clear_invalid_company_addresses,
    _external_pi_candidates,
    _normalize_payment_fields,
    _normalize_payment_schedule_dates,
    _order_purchase_invoices_for_sync,
    _resolve_effective_external_pi_return_parent,
    _sanitize_purchase_invoice_header,
    _set_source_posting_datetime,
    _sync_pi_item_with_po_reference,
    _sync_pi_amendment_parent,
    get_po_detail,
)


MODULE = "emkan_insights.emkan_insights.external_pi_sync"


class TestExternalPurchaseInvoiceSync(FrappeTestCase):
    @patch(f"{MODULE}.get_due_date_from_template", return_value="2026-07-30")
    def test_caps_schedule_date_at_local_template_limit(self, get_due_date):
        pi = frappe._dict(
            payment_terms_template="100% ADVANCED PAYMENT",
            posting_date="2026-08-01",
            bill_date="2026-07-30",
            due_date="2026-08-01",
            payment_schedule=[frappe._dict(due_date="2026-08-01")],
        )

        _normalize_payment_schedule_dates(pi)

        self.assertEqual(str(pi.due_date), "2026-07-30")
        self.assertEqual(str(pi.payment_schedule[0].due_date), "2026-07-30")
        get_due_date.assert_called_once_with(
            "100% ADVANCED PAYMENT",
            "2026-08-01",
            "2026-07-30",
        )

    def test_preserves_source_posting_date_and_time(self):
        ext_pi = frappe._dict(
            name="EXT-PI-1",
            posting_date="2026-09-01",
            posting_time="11:29:02",
        )
        pi = frappe._dict(
            set_posting_time=0,
            posting_date="2026-08-30",
            posting_time="00:00:00",
        )

        _set_source_posting_datetime(pi, ext_pi)

        self.assertEqual(pi.set_posting_time, 1)
        self.assertEqual(pi.posting_date, "2026-09-01")
        self.assertEqual(pi.posting_time, "11:29:02")

    def test_full_payment_uses_recalculated_total_without_write_off(self):
        ext_pi = frappe._dict(
            is_paid=1,
            paid_amount=10.01,
            grand_total=10.01,
            rounded_total=0,
            write_off_amount=0,
        )
        ext_pi.precision = lambda _fieldname: 2
        pi = frappe._dict(
            is_paid=1,
            paid_amount=10.01,
            base_paid_amount=10.01,
            grand_total=10.00,
            rounded_total=0,
            conversion_rate=1,
            write_off_amount=0.01,
            base_write_off_amount=0.01,
            write_off_account="Write Off - CPC",
            write_off_cost_center="Main - CPC",
        )
        pi.precision = lambda _fieldname: 2

        _normalize_payment_fields(pi, ext_pi)

        self.assertEqual(pi.paid_amount, 10.0)
        self.assertEqual(pi.base_paid_amount, 10.0)
        self.assertEqual(pi.write_off_amount, 0)
        self.assertEqual(pi.base_write_off_amount, 0)
        self.assertIsNone(pi.write_off_account)
        self.assertIsNone(pi.write_off_cost_center)

    def test_partial_payment_is_not_changed(self):
        ext_pi = frappe._dict(
            is_paid=1,
            paid_amount=4,
            grand_total=10,
            rounded_total=0,
            write_off_amount=0,
        )
        ext_pi.precision = lambda _fieldname: 2
        pi = frappe._dict(
            is_paid=1,
            paid_amount=4,
            base_paid_amount=4,
            grand_total=9.99,
            rounded_total=0,
            conversion_rate=1,
        )
        pi.precision = lambda _fieldname: 2

        _normalize_payment_fields(pi, ext_pi)

        self.assertEqual(pi.paid_amount, 4)
        self.assertEqual(pi.base_paid_amount, 4)

    @patch(f"{MODULE}.custom_validate_company_linked_addresses")
    def test_clears_invalid_company_addresses_before_insert(self, validate):
        pi = frappe._dict(
            doctype="Purchase Invoice",
            name="PI-1",
            company="Construction Pillars Company",
            billing_address=None,
            shipping_address="QBUA2539-Billing-1",
        )

        _clear_invalid_company_addresses(pi)

        validate.assert_called_once_with(pi)

    def test_resolves_po_item_by_remote_child_id(self):
        po_item_meta = frappe._dict()
        po_item_meta.has_field = lambda fieldname: fieldname == "custom_remote_id"

        with (
            patch(f"{MODULE}.frappe.db.exists", return_value=False),
            patch(f"{MODULE}.frappe.get_meta", return_value=po_item_meta),
            patch(
                f"{MODULE}.frappe.db.get_value",
                return_value="LOCAL-PO-ITEM-2",
            ) as get_value,
        ):
            result = get_po_detail(
                "PO-1", "ITEM-1", remote_po_item="REMOTE-PO-ITEM-2"
            )

        self.assertEqual(result, "LOCAL-PO-ITEM-2")
        get_value.assert_called_once_with(
            "Purchase Order Item",
            {
                "parent": "PO-1",
                "custom_remote_id": "REMOTE-PO-ITEM-2",
            },
            "name",
        )

    @patch(f"{MODULE}.frappe.db.get_value")
    def test_inherits_validated_fields_from_po_item(self, get_value):
        get_value.return_value = frappe._dict(
            project="PROJ-0012",
            item_code="02010839",
            uom="PCS",
        )
        item_row = {
            "po_detail": "LOCAL-PO-ITEM-2",
            "project": None,
            "item_code": "02010839",
            "uom": "PCS",
        }

        _sync_pi_item_with_po_reference(item_row)

        self.assertEqual(item_row["project"], "PROJ-0012")
        self.assertEqual(item_row["item_code"], "02010839")
        self.assertEqual(item_row["uom"], "PCS")
        get_value.assert_called_once_with(
            "Purchase Order Item",
            "LOCAL-PO-ITEM-2",
            ["project", "item_code", "uom"],
            as_dict=True,
        )

    @patch(f"{MODULE}._log")
    @patch(f"{MODULE}.frappe.db.exists", return_value=False)
    def test_invalid_supplier_group_is_cleared(self, exists, log):
        pi = frappe._dict(
            name="PI-1",
            supplier_group="Supplier",
        )

        _sanitize_purchase_invoice_header(pi)

        self.assertIsNone(pi.supplier_group)
        exists.assert_called_once_with("Supplier Group", "Supplier")
        log.assert_called_once()

    def test_external_pi_candidates_add_source_prefix(self):
        self.assertEqual(
            _external_pi_candidates("ACC-PINV-1"),
            ["EHC-ACC-PINV-1", "ACC-PINV-1"],
        )
        self.assertEqual(
            _external_pi_candidates("EHC-ACC-PINV-1"),
            ["EHC-ACC-PINV-1"],
        )

    @patch(f"{MODULE}.frappe.get_all")
    def test_orders_originals_then_amendments_then_returns(self, get_all):
        get_all.return_value = [
            frappe._dict(
                name="RETURN-1",
                is_return=1,
                return_against="PI-1",
                amended_from=None,
            ),
            frappe._dict(
                name="PI-1-1",
                is_return=0,
                return_against=None,
                amended_from="PI-1",
            ),
        ]

        result = _order_purchase_invoices_for_sync(
            ["RETURN-1", "PI-1-1", "PI-1"]
        )

        self.assertEqual(result, ["PI-1", "PI-1-1", "RETURN-1"])

    @patch(f"{MODULE}.frappe.get_all")
    def test_return_uses_matching_active_amendment(self, get_all):
        get_all.return_value = [
            frappe._dict(
                name="PI-1-1",
                docstatus=1,
                supplier="SUP-1",
                company="Company",
                currency="SAR",
            )
        ]
        ext_return = frappe._dict(
            supplier="SUP-1",
            company="Company",
            currency="SAR",
        )

        result = _resolve_effective_external_pi_return_parent(
            ext_return, "PI-1"
        )

        self.assertEqual(result, "PI-1-1")

    def test_amendment_parent_is_force_cancelled_before_amendment(self):
        ext_pi = frappe._dict(name="PI-1-1", amended_from="PI-1")

        with (
            patch(
                f"{MODULE}._resolve_cancelled_local_pinv",
                side_effect=[None, "CPC-EHC-PI-1"],
            ),
            patch(
                f"{MODULE}._resolve_external_purchase_invoice",
                return_value="EHC-PI-1",
            ),
            patch(f"{MODULE}._sync_single_purchase_invoice") as sync_one,
        ):
            result = _sync_pi_amendment_parent(
                "External Purchase Invoice", ext_pi, "CPC", ()
            )

        self.assertEqual(result, "CPC-EHC-PI-1")
        sync_one.assert_called_once_with(
            "External Purchase Invoice",
            "EHC-PI-1",
            dependency_stack=("PI-1-1",),
            force_cancellation=True,
        )
