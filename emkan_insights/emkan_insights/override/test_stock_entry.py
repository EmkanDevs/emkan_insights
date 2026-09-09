from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.override.stock_entry import StockEntry


class TestStockEntry(FrappeTestCase):
	@patch("frappe.db.sql")
	@patch("frappe.db.escape", side_effect=lambda value: repr(value))
	@patch("frappe.get_value", return_value=None)
	@patch("frappe.get_all", return_value=[frappe._dict(qty=5.0)])
	def test_missing_requested_transfer_qty_uses_current_row_qty(
		self,
		_get_all,
		_get_value,
		_escape,
		db_sql,
	):
		doc = MagicMock(
			purpose="Material Transfer",
			outgoing_stock_entry="SOURCE-ENTRY",
			items=[
				frappe._dict(
					against_stock_entry="SOURCE-ENTRY",
					ste_detail="MISSING-SOURCE-ROW",
					transfer_qty="5",
					docstatus=1,
					idx=1,
				)
			],
		)

		StockEntry.update_transferred_qty(doc)

		db_sql.assert_called_once()
		doc._update_percent_field_in_targets.assert_called_once()
