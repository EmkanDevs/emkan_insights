from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services import (
	_item_group_hold_reason,
	_order_sync_hold_reason,
	_organization_link_hold_reason,
	_partner_link_hold_reason,
	_sync_hold_reason,
)


class TestOrderHolds(FrappeTestCase):
	def test_purchase_order_with_earlier_required_date_is_held(self):
		mapping = SimpleNamespace(name="SAP Purchase Order")
		record = {
			"DocDate": "2025-02-16T00:00:00Z",
			"DocumentLines": [{"ShipDate": "2025-02-12T00:00:00Z"}],
		}
		self.assertEqual(
			_order_sync_hold_reason(mapping, record),
			"SAP line required date precedes the order transaction date",
		)

	def test_sales_order_without_item_stock_uom_is_held(self):
		mapping = SimpleNamespace(name="SAP Sales Order")
		record = {"DocumentLines": [{"ItemCode": "ITEM-1", "UoMCode": "EACH"}]}
		with patch("frappe.db.get_value", return_value=SimpleNamespace(
			stock_uom=None, is_sales_item=1
		)):
			self.assertEqual(
				_order_sync_hold_reason(mapping, record),
				"ERP sales item needs a sales flag and stock UOM",
			)

	def test_sales_order_with_valid_uom_is_ready(self):
		mapping = SimpleNamespace(name="SAP Sales Order")
		record = {"DocumentLines": [{"ItemCode": "ITEM-1", "UoMCode": "EACH"}]}
		with patch("frappe.db.get_value", return_value=SimpleNamespace(
			stock_uom="EACH", is_sales_item=1
		)):
			self.assertIsNone(_order_sync_hold_reason(mapping, record))

	def test_item_with_broken_group_link_is_held(self):
		mapping = SimpleNamespace(name="SAP Item")
		with patch("frappe.db.get_value", return_value={"item_group": "CONSUMABLES"}):
			with patch("frappe.db.exists", side_effect=[True, False]):
				self.assertEqual(
					_item_group_hold_reason(mapping, {"ItemCode": "ITEM-1", "ItemsGroupCode": 104}),
					"Existing ERP Item item_group link needs repair",
				)

	def test_stock_document_with_broken_item_group_is_held(self):
		mapping = SimpleNamespace(name="SAP Purchase Receipt")
		with patch("frappe.db.get_value", return_value=SimpleNamespace(
			item_group="CONSUMABLES", stock_uom="EACH"
		)):
			with patch("frappe.db.exists", return_value=False):
				self.assertEqual(
					_item_group_hold_reason(mapping, {
						"DocumentLines": [{"ItemCode": "ITEM-1"}],
					}),
					"ERP stock item needs a valid Item Group link",
					)

	def test_purchase_receipt_with_broken_supplier_price_list_is_held(self):
		mapping = SimpleNamespace(name="SAP Purchase Receipt")
		with patch("frappe.db.get_value", return_value={
			"supplier_group": "101", "default_price_list": "Price List 01",
			"default_currency": "SAR",
		}):
			with patch("frappe.db.exists", side_effect=lambda doctype, value: value != "Price List 01"):
				self.assertEqual(
					_partner_link_hold_reason(mapping, {"CardCode": "SUP-1"}),
					"ERP Supplier default_price_list link needs repair",
				)

	def test_supplier_group_102_uses_existing_services_group(self):
		mapping = SimpleNamespace(name="SAP Supplier")
		record = {"CardCode": "SUP-1", "GroupCode": 102, "PriceListNum": 1}
		with patch("frappe.db.exists", return_value=True):
			with patch("frappe.db.get_value", return_value=None):
				self.assertIsNone(_partner_link_hold_reason(mapping, record))

	def test_misdirected_existing_blanket_order_is_held(self):
		mapping = SimpleNamespace(name="SAP Blanket Order")
		with patch("frappe.db.get_value", return_value=SimpleNamespace(
			customer="V00012", blanket_order_type=None
		)):
			self.assertEqual(
				_sync_hold_reason(mapping, {"AgreementNo": 52, "BPCode": "V00012"}),
				"Existing ERP Blanket Order needs purchasing direction repair",
			)

	def test_employee_with_unmapped_job_title_is_held(self):
		mapping = SimpleNamespace(name="SAP Employee")
		with patch("frappe.db.get_value", return_value=None):
			self.assertEqual(
				_organization_link_hold_reason(mapping, {"EmployeeID": 104, "JobTitle": "New Title"}),
				"SAP Employee job title needs an ERP Designation",
			)

	def test_profit_center_group_without_erp_parent_is_held(self):
		mapping = SimpleNamespace(name="SAP Cost Center")
		with patch("frappe.db.exists", return_value=False):
			self.assertEqual(
				_organization_link_hold_reason(mapping, {"CenterCode": "4", "GroupCode": "FN"}),
				"SAP Profit Center group needs an ERP Cost Center grouping rule",
			)

	def test_journal_entry_is_held_during_account_repair(self):
		self.assertEqual(
			_sync_hold_reason(SimpleNamespace(name="SAP Journal Entry"), {"JdtNum": 42}),
			"ERP Journal Entry accounts and Account hierarchy need repair",
		)
