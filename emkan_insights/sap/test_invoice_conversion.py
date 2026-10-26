from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services import _invoice_sync_hold_reason
from emkan_insights.sap.services.transforms import item_conversion_factor


class TestInvoiceConversion(FrappeTestCase):
	def test_uses_item_uom_conversion(self):
		context = {"source": {"ItemCode": "ITEM-1", "UoMCode": "BOX"}}
		with patch("frappe.db.get_value", return_value=12):
			self.assertEqual(item_conversion_factor(context), 12)

	def test_rejects_missing_item_uom_conversion(self):
		context = {"source": {"ItemCode": "ITEM-1", "UoMCode": "BOX"}}
		with patch("frappe.db.get_value", side_effect=[None, "EACH"]):
			with self.assertRaisesRegex(frappe.ValidationError, "conversion is missing"):
				item_conversion_factor(context)

	def test_purchase_invoice_holds_item_without_purchase_flag(self):
		mapping = SimpleNamespace(name="SAP Purchase Invoice")
		source = {
			"DocType": "dDocument_Items",
			"DocumentLines": [{"ItemCode": "ITEM-1", "UoMCode": "EACH"}],
		}
		with patch("frappe.db.get_value", return_value=SimpleNamespace(
			is_purchase_item=0, stock_uom="EACH"
		)):
			self.assertEqual(
				_invoice_sync_hold_reason(mapping, source),
				"ERP item needs a purchase flag and stock UOM",
			)
