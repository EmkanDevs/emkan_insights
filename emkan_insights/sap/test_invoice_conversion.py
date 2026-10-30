from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.transforms import (
	item_conversion_factor,
	purchase_invoice_item_description,
	purchase_invoice_item_quantity,
	sap_item_uom,
)


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

	def test_service_line_uses_account_description_and_one_unit(self):
		context = {
			"company": "Emkan",
			"parent_source": {"DocType": "dDocument_Service"},
			"source": {"AccountCode": "6221204", "Quantity": 0, "ItemCode": None},
		}
		with patch("frappe.db.get_value", return_value="Service Expense"):
			self.assertEqual(purchase_invoice_item_description(None, context), "Service Expense")
		self.assertEqual(purchase_invoice_item_quantity(0, context), 1)
		self.assertEqual(sap_item_uom(None, {"transform_config": {"service_uom_fallback": True}}, context), "Nos")
		self.assertEqual(item_conversion_factor(context), 1)

	def test_manual_item_uom_uses_stock_uom(self):
		context = {"source": {"ItemCode": "SERVICE-1", "UoMCode": "Manual", "UnitsOfMeasurment": 1}}
		rule = {"transform_config": {"manual_uom_fallback": True}}
		with patch("frappe.db.get_value", side_effect=["Nos", 1]):
			self.assertEqual(item_conversion_factor(context, rule), 1)
