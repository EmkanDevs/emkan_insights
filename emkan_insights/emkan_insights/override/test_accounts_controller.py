from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from erpnext.controllers.accounts_controller import AccountsController
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt

from emkan_insights import hooks as _hooks  # noqa: F401
from emkan_insights.emkan_insights.override.accounts_controller import (
	custom_validate_company_linked_addresses,
	custom_validate_party_address_and_contact,
)


class TransactionStub:
	def __init__(self, doctype, party_type=None, party=None, **fields):
		self.doctype = doctype
		self.party_type = party_type
		self.party = party
		self.fields = fields
		self.company = fields.get("company")
		self.items = fields.get("items", [])
		self.drop_ship = fields.pop("drop_ship", False)

	def get(self, fieldname):
		return self.fields.get(fieldname)

	def set(self, fieldname, value):
		self.fields[fieldname] = value

	def get_party(self):
		return self.party_type, self.party

	def is_drop_ship(self, _items):
		return self.drop_ship


class TestCustomAccountsController(FrappeTestCase):
	def test_methods_are_installed_on_shared_controller(self):
		self.assertIs(
			AccountsController.validate_company_linked_addresses,
			custom_validate_company_linked_addresses,
		)
		self.assertIs(
			AccountsController.validate_party_address_and_contact,
			custom_validate_party_address_and_contact,
		)
		self.assertIs(
			PurchaseReceipt.validate_company_linked_addresses,
			custom_validate_company_linked_addresses,
		)

	@patch("emkan_insights.emkan_insights.override.accounts_controller.frappe.db.exists")
	def test_purchase_receipt_clears_company_address(self, exists):
		exists.return_value = False
		doc = PurchaseReceipt(
			{
				"doctype": "Purchase Receipt",
				"company": "Example Company",
				"billing_address": "Wrong Billing Address",
			}
		)

		doc.validate_company_linked_addresses()

		self.assertIsNone(doc.billing_address)

	@patch("emkan_insights.emkan_insights.override.accounts_controller.frappe.db.exists")
	def test_clears_only_company_addresses_not_linked_to_company(self, exists):
		exists.side_effect = lambda _doctype, filters: filters["parent"] == "Valid Company Address"
		doc = TransactionStub(
			"Sales Invoice",
			company="Example Company",
			dispatch_address_name="Wrong Dispatch Address",
			company_address="Valid Company Address",
		)

		custom_validate_company_linked_addresses(doc)

		self.assertIsNone(doc.get("dispatch_address_name"))
		self.assertEqual(doc.get("company_address"), "Valid Company Address")

	@patch("emkan_insights.emkan_insights.override.accounts_controller.frappe.db.exists")
	def test_preserves_drop_ship_address(self, exists):
		doc = TransactionStub(
			"Purchase Order",
			company="Example Company",
			billing_address="Valid Billing Address",
			shipping_address="Customer Shipping Address",
			drop_ship=True,
		)
		exists.return_value = True

		custom_validate_company_linked_addresses(doc)

		self.assertEqual(doc.get("shipping_address"), "Customer Shipping Address")
		exists.assert_called_once()

	@patch("emkan_insights.emkan_insights.override.accounts_controller.frappe.get_all")
	def test_clears_customer_addresses_and_contact_not_linked_to_customer(self, get_all):
		get_all.side_effect = [["Valid Billing Address"], ["Valid Contact"]]
		doc = TransactionStub(
			"Sales Order",
			party_type="Customer",
			party="Customer A",
			customer_address="Valid Billing Address",
			shipping_address_name="Wrong Shipping Address",
			contact_person="Wrong Contact",
		)

		custom_validate_party_address_and_contact(doc)

		self.assertEqual(doc.get("customer_address"), "Valid Billing Address")
		self.assertIsNone(doc.get("shipping_address_name"))
		self.assertIsNone(doc.get("contact_person"))

	@patch("emkan_insights.emkan_insights.override.accounts_controller.frappe.get_all")
	def test_clears_supplier_address_not_linked_to_supplier(self, get_all):
		get_all.side_effect = [[], []]
		doc = TransactionStub(
			"Purchase Invoice",
			party_type="Supplier",
			party="Supplier A",
			supplier_address="Wrong Supplier Address",
		)

		custom_validate_party_address_and_contact(doc)

		self.assertIsNone(doc.get("supplier_address"))
