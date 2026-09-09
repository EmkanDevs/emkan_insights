from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.sync_guard import (
    active_item_during_opening_stock,
    enable_supplier_for_sync,
    party_validation_disabled,
    preserve_copied_outgoing_rates,
    temporarily_enable_stock_entry_warehouses,
)


class TestPartyValidationDisabled(FrappeTestCase):
    def test_restores_party_validation_flag(self):
        previous_value = frappe.flags.ignore_party_validation

        with self.assertRaisesRegex(RuntimeError, "sync failed"):
            with party_validation_disabled():
                self.assertTrue(frappe.flags.ignore_party_validation)
                raise RuntimeError("sync failed")

        self.assertEqual(frappe.flags.ignore_party_validation, previous_value)


class TestEnableSupplierForSync(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.clear_document_cache")
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.set_value")
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.get_value")
    def test_enables_disabled_supplier(self, get_value, set_value, clear_cache):
        get_value.return_value = {"disabled": 1, "is_frozen": 0}

        changed = enable_supplier_for_sync("SUP-0850")

        self.assertTrue(changed)
        set_value.assert_called_once_with("Supplier", "SUP-0850", {"disabled": 0})
        clear_cache.assert_called_once_with("Supplier", "SUP-0850")

    @patch("emkan_insights.emkan_insights.sync_guard.frappe.clear_document_cache")
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.set_value")
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.get_value")
    def test_does_not_write_active_supplier(self, get_value, set_value, clear_cache):
        get_value.return_value = {"disabled": 0, "is_frozen": 0}

        changed = enable_supplier_for_sync("SUP-0850")

        self.assertFalse(changed)
        set_value.assert_not_called()
        clear_cache.assert_not_called()


class TestPreserveCopiedOutgoingRates(FrappeTestCase):
    @patch(
        "emkan_insights.emkan_insights.sync_guard.frappe.db.get_value",
        return_value=frappe._dict(valuation_rate=0.01, basic_rate=0.01),
    )
    def test_uses_copied_item_valuation_as_stock_ledger_fallback(self, get_value):
        from erpnext.stock.stock_ledger import update_entries_after

        ledger = object.__new__(update_entries_after)
        sle = frappe._dict(
            voucher_type="Stock Entry",
            voucher_detail_no="STOCK-ENTRY-DETAIL-1",
        )
        original_get_fallback_rate = update_entries_after.get_fallback_rate

        with preserve_copied_outgoing_rates():
            self.assertEqual(ledger.get_fallback_rate(sle), 0.01)

        self.assertIs(
            update_entries_after.get_fallback_rate,
            original_get_fallback_rate,
        )
        get_value.assert_called_once_with(
            "Stock Entry Detail",
            "STOCK-ENTRY-DETAIL-1",
            ["valuation_rate", "basic_rate"],
            as_dict=True,
        )


class TestActiveItemDuringOpeningStock(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.set_value")
    def test_restores_inactive_fields_after_insert(self, set_value):
        item = MagicMock(opening_stock=5, disabled=1, end_of_life="2026-08-31")
        item.name = "ITEM-001"
        item.is_new.side_effect = [True, False]

        with active_item_during_opening_stock(item):
            self.assertEqual(item.disabled, 0)
            self.assertIsNone(item.end_of_life)

        self.assertEqual(item.disabled, 1)
        self.assertEqual(item.end_of_life, "2026-08-31")
        set_value.assert_called_once_with(
            "Item",
            "ITEM-001",
            {"disabled": 1, "end_of_life": "2026-08-31"},
            update_modified=False,
        )

    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.set_value")
    def test_does_not_change_item_without_opening_stock(self, set_value):
        item = MagicMock(
            opening_stock=0,
            disabled=1,
            end_of_life="2026-08-31",
        )
        item.is_new.return_value = True

        with active_item_during_opening_stock(item):
            self.assertEqual(item.disabled, 1)
            self.assertEqual(item.end_of_life, "2026-08-31")

        set_value.assert_not_called()


class TestTemporarilyEnableStockEntryWarehouses(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.set_value")
    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.get_value")
    def test_restores_disabled_warehouses_after_failure(self, get_value, set_value):
        doc = frappe._dict(
            from_warehouse="HEADER-SOURCE",
            to_warehouse="HEADER-TARGET",
            items=[
                frappe._dict(
                    s_warehouse="ROW-SOURCE",
                    t_warehouse="ROW-TARGET",
                )
            ],
        )
        get_value.side_effect = lambda _doctype, warehouse, _field: warehouse in {
            "HEADER-SOURCE",
            "ROW-TARGET",
        }

        with self.assertRaisesRegex(RuntimeError, "stock entry failed"):
            with temporarily_enable_stock_entry_warehouses(doc):
                raise RuntimeError("stock entry failed")

        set_value.assert_has_calls(
            [
                call(
                    "Warehouse",
                    "HEADER-SOURCE",
                    "disabled",
                    0,
                    update_modified=False,
                ),
                call(
                    "Warehouse",
                    "ROW-TARGET",
                    "disabled",
                    0,
                    update_modified=False,
                ),
                call(
                    "Warehouse",
                    "HEADER-SOURCE",
                    "disabled",
                    1,
                    update_modified=False,
                ),
                call(
                    "Warehouse",
                    "ROW-TARGET",
                    "disabled",
                    1,
                    update_modified=False,
                ),
            ],
            any_order=True,
        )
        self.assertEqual(set_value.call_count, 4)

    @patch("emkan_insights.emkan_insights.sync_guard.frappe.db.set_value")
    @patch(
        "emkan_insights.emkan_insights.sync_guard.frappe.db.get_value",
        return_value=0,
    )
    def test_does_not_write_enabled_warehouses(self, _get_value, set_value):
        doc = frappe._dict(
            from_warehouse=None,
            to_warehouse=None,
            items=[frappe._dict(s_warehouse="ACTIVE", t_warehouse=None)],
        )

        with temporarily_enable_stock_entry_warehouses(doc):
            pass

        set_value.assert_not_called()
