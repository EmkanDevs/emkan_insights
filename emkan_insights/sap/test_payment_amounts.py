import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.mapper import Mapper


class TestPaymentAmounts(FrappeTestCase):
	def setUp(self):
		super().setUp()
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		rule = next(row for row in catalog if row["name"] == "SAP Payment Entry Incoming")
		self.mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry", field_mapping={"paid_amount": rule["field_mapping"]["paid_amount"]},
		))

	def test_cash_amount_is_not_hidden_by_zero_transfer(self):
		self.assertEqual(self.mapper.map({"TransferSum": 0, "CashSum": 25})["paid_amount"], 25)

	def test_payment_methods_are_added(self):
		self.assertEqual(self.mapper.map({
			"TransferSum": 10, "CashSum": 5, "CheckSum": 2.5, "CreditSum": 0,
		})["paid_amount"], 17.5)

	def test_missing_amount_is_not_invented(self):
		self.assertIsNone(self.mapper.map({})["paid_amount"])

	def test_invalid_amount_fails(self):
		with self.assertRaisesRegex(frappe.ValidationError, "must contain numbers"):
			self.mapper.map({"CashSum": "invalid"})

	def test_party_type_follows_sap_payment_type(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		for name, sap_type, expected in (
			("SAP Payment Entry Incoming", "rCustomer", "Customer"),
			("SAP Payment Entry Incoming", "rSupplier", "Supplier"),
			("SAP Payment Entry Outgoing", "rSupplier", "Supplier"),
		):
			with self.subTest(mapping=name, sap_type=sap_type):
				rule = next(row for row in catalog if row["name"] == name)["field_mapping"]["party_type"]
				mapper = Mapper(SimpleNamespace(
					erp_doctype="Payment Entry", field_mapping={"party_type": rule},
				))
				self.assertEqual(mapper.map({"DocType": sap_type})["party_type"], expected)

	def test_account_payment_is_not_mapped_as_customer(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		rule = next(row for row in catalog if row["name"] == "SAP Payment Entry Incoming")
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry", field_mapping={"party_type": rule["field_mapping"]["party_type"]},
		))
		with self.assertRaisesRegex(frappe.ValidationError, "No conditional_value match"):
			mapper.map({"DocType": "rAccount"})

	def test_payment_method_account_uses_the_positive_method(self):
		rule = {"transform": "payment_method_account"}
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry", field_mapping={"paid_to": rule},
		), {"company": "Emkan"})
		with patch("frappe.db.get_value", return_value="Bank - EM") as get_value:
			self.assertEqual(mapper.map({
				"TransferSum": 0, "TransferAccount": "TRANSFER",
				"CashSum": 25, "CashAccount": "CASH",
			})["paid_to"], "Bank - EM")
			get_value.assert_called_once_with(
				"Account", {"account_number": "CASH", "company": "Emkan"}, "name"
			)

	def test_payment_method_account_rejects_mixed_methods(self):
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry",
			field_mapping={"paid_to": {"transform": "payment_method_account"}},
		), {"company": "Emkan"})
		with self.assertRaisesRegex(frappe.ValidationError, "one supported cash or transfer method"):
			mapper.map({"TransferSum": 10, "CashSum": 5})

	def test_local_payment_rate_and_base_amount(self):
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry",
			field_mapping={
				"source_exchange_rate": {"transform": "local_payment_value", "transform_config": {"kind": "rate"}},
				"base_paid_amount": {"transform": "local_payment_value", "transform_config": {"kind": "amount"}},
			},
		), {"company": "Emkan", "currency": "SAR"})
		with patch("frappe.db.get_value", return_value="SAR"):
			mapped = mapper.map({
				"DocCurrency": "SAR", "ControlAccount": "PARTY",
				"TransferSum": 25, "TransferAccount": "BANK", "CashSum": 0,
			})
		self.assertEqual(mapped["source_exchange_rate"], 1)
		self.assertEqual(mapped["base_paid_amount"], 25)

	def test_foreign_currency_payment_requires_separate_rule(self):
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry",
			field_mapping={
				"source_exchange_rate": {"transform": "local_payment_value", "transform_config": {"kind": "rate"}},
			},
		), {"company": "Emkan", "currency": "SAR"})
		with self.assertRaisesRegex(frappe.ValidationError, "foreign currency mapping"):
			mapper.map({"DocCurrency": "USD"})
