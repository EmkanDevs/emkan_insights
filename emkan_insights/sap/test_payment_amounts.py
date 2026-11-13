import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.services.references import SAPReferenceResolver
from emkan_insights.sap.services.transforms import payment_ledger_amount


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
		mapped = self.mapper.map({"CashSum": "invalid"})
		self.assertIsNone(mapped["paid_amount"])
		self.assertIn("must contain numbers", mapped["_sap_mapping_errors"][0]["message"])

	def test_party_type_follows_sap_payment_type(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		for name, sap_type, expected in (
			("SAP Payment Entry Incoming", "rCustomer", "Customer"),
			("SAP Payment Entry Incoming", "rSupplier", "Supplier"),
			("SAP Payment Entry Outgoing", "rSupplier", "Supplier"),
			("SAP Payment Entry Outgoing", "rCustomer", "Customer"),
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
		mapped = mapper.map({"DocType": "rAccount"})
		self.assertIsNone(mapped["party_type"])
		self.assertNotIn("_sap_mapping_errors", mapped)

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
		mapped = mapper.map({"TransferSum": 10, "CashSum": 5})
		self.assertIsNone(mapped["paid_to"])
		self.assertIn("one supported cash or transfer method", mapped["_sap_mapping_errors"][0]["message"])

	def test_local_payment_rate_and_base_amount(self):
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Payment Entry",
			field_mapping={
				"source_exchange_rate": {"transform": "local_payment_value", "transform_config": {"kind": "rate", "incoming": True}},
				"base_paid_amount": {"transform": "local_payment_value", "transform_config": {"kind": "amount", "incoming": True}},
			},
		), {"company": "Emkan", "currency": "SAR"})
		with patch("frappe.db.get_value", return_value="SAR"):
			mapped = mapper.map({
				"DocCurrency": "SAR", "DocType": "rCustomer", "ControlAccount": "PARTY",
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
		mapped = mapper.map({"DocCurrency": "USD"})
		self.assertIsNone(mapped["source_exchange_rate"])
		self.assertIn("foreign currency mapping", mapped["_sap_mapping_errors"][0]["message"])

	def test_single_invoice_overstatement_uses_verified_payment_amount(self):
		invoice = {"DocEntry": 77, "InvoiceType": "it_Invoice", "SumApplied": 150}
		payment = {
			"DocEntry": 42, "DocNum": 99, "DocType": "rCustomer", "CardCode": "C-1",
			"DocDate": "2026-10-22", "DocCurrency": "SAR", "TransferSum": 100,
			"PaymentInvoices": [invoice],
		}
		resolver = Mock()
		context = {"source": payment, "currency": "SAR", "sap_references": resolver}
		payment_rule = {"transform_config": {"source_kind": "payment", "currency_kind": "company"}}
		allocation_rule = {"transform_config": {"source_kind": "allocation", "currency_kind": "company"}}
		self.assertEqual(payment_ledger_amount(payment_rule, context), 0)
		line = {**{f"__parent_{key}": value for key, value in payment.items()}, **invoice}
		self.assertEqual(payment_ledger_amount(allocation_rule, {**context, "source": line}), -100)
		resolver.verify_single_invoice_payment.assert_any_call(
			payment, invoice, Decimal("150"), Decimal("100")
		)

	def test_journal_verification_rejects_missing_party_posting(self):
		client = Mock()
		client.get.side_effect = lambda entity, params: {
			"value": [{params["$select"]: 150 if params["$select"] == "DocTotal" else "C-1"}],
		}
		client.fetch_all.return_value = [{
			"ReferenceDate": "2026-10-22T00:00:00Z",
			"Memo": "Incoming Payments - C-1",
			"JournalEntryLines": [{"ShortName": "C-1", "Credit": 90}],
		}]
		resolver = SAPReferenceResolver(client)
		with self.assertRaisesRegex(frappe.ValidationError, "one matching journal"):
			resolver.verify_single_invoice_payment(
				{"DocEntry": 42, "DocNum": 99, "CardCode": "C-1", "DocDate": "2026-10-22"},
				{"DocEntry": 77}, Decimal("150"), Decimal("100"),
			)

	def test_vendor_invoice_overstatement_uses_verified_supplier_debit(self):
		client = Mock()
		client.get.side_effect = lambda entity, params: {
			"value": [{params["$select"]: 150 if params["$select"] == "DocTotal" else "V-1"}],
		}
		client.fetch_all.return_value = [{
			"ReferenceDate": "2026-10-22T00:00:00Z",
			"Memo": "Outgoing Payments - V-1",
			"JournalEntryLines": [{"ShortName": "V-1", "Debit": 100, "Credit": 0}],
		}]
		invoice = {"DocEntry": 77, "InvoiceType": "it_PurchaseInvoice", "SumApplied": 150}
		payment = {
			"DocEntry": 42, "DocNum": 99, "DocType": "rSupplier", "CardCode": "V-1",
			"DocDate": "2026-10-22", "DocCurrency": "SAR", "TransferSum": 100,
			"PaymentInvoices": [invoice],
		}
		context = {"source": payment, "currency": "SAR", "sap_references": SAPReferenceResolver(client)}
		payment_rule = {"transform_config": {"source_kind": "payment", "currency_kind": "company"}}
		allocation_rule = {"transform_config": {"source_kind": "allocation", "currency_kind": "company"}}
		self.assertEqual(payment_ledger_amount(payment_rule, context), 0)
		line = {**{f"__parent_{key}": value for key, value in payment.items()}, **invoice}
		self.assertEqual(payment_ledger_amount(allocation_rule, {**context, "source": line}), -100)
		self.assertEqual(client.get.call_args_list[0].args[0], "PurchaseInvoices")

	def test_negative_purchase_invoice_requires_matching_posted_supplier_debit(self):
		client = Mock()
		client.get.return_value = {"value": [{
			"ReferenceDate": "2025-04-15T00:00:00Z", "Memo": "A/P Invoices - V00318",
			"JournalEntryLines": [{"ShortName": "V00318", "Debit": 2633.55, "Credit": 0}],
		}]}
		invoice = {
			"DocEntry": 1640, "DocTotal": -2633.55, "TransNum": 9916,
			"CardCode": "V00318", "DocDate": "2025-04-15", "Cancelled": "tNO",
			"JournalMemo": "A/P Invoices - V00318",
		}
		context = {"source": invoice, "currency": "SAR", "sap_references": SAPReferenceResolver(client)}
		rule = {"transform_config": {
			"source_kind": "invoice", "currency_kind": "company", "verify_negative_invoice": True,
		}}
		self.assertEqual(payment_ledger_amount(rule, context), -2633.55)
		client.get.assert_called_once()
		client.get.return_value["value"][0]["JournalEntryLines"][0]["Debit"] = 2600
		with self.assertRaisesRegex(frappe.ValidationError, "does not match"):
			payment_ledger_amount(rule, {**context, "sap_references": SAPReferenceResolver(client)})
		client.get.return_value["value"][0].update({
			"Memo": "A/P Invoice - Cancellation - V00318",
			"JournalEntryLines": [{"ShortName": "V00318", "Debit": 0, "Credit": 2633.55}],
		})
		invoice["JournalMemo"] = "A/P Invoice - Cancellation - V00318"
		self.assertEqual(payment_ledger_amount(rule, {**context, "sap_references": SAPReferenceResolver(client)}), 2633.55)

	def test_mixed_supplier_payment_adjusts_sole_positive_allocation(self):
		journal = {"LineNum": 0, "DocEntry": 7988, "InvoiceType": "it_JournalEntry", "SumApplied": 391552}
		advice = {"LineNum": 1, "DocEntry": 7449, "InvoiceType": "it_PaymentAdvice", "SumApplied": -44965}
		payment = {
			"DocEntry": 1621, "DocNum": 24001023, "DocType": "rSupplier",
			"DocCurrency": "SAR", "TransferSum": 108054,
			"PaymentInvoices": [journal, advice],
		}
		resolver = Mock()
		context = {"source": payment, "currency": "SAR", "sap_references": resolver}
		payment_rule = {"transform_config": {"source_kind": "payment", "currency_kind": "company"}}
		allocation_rule = {"transform_config": {"source_kind": "allocation", "currency_kind": "company"}}
		self.assertEqual(payment_ledger_amount(payment_rule, context), 0)
		parent = {f"__parent_{key}": value for key, value in payment.items()}
		self.assertEqual(payment_ledger_amount(allocation_rule, {**context, "source": {**parent, **journal}}), -153019)
		self.assertEqual(payment_ledger_amount(allocation_rule, {**context, "source": {**parent, **advice}}), 44965)
		resolver.verify_outgoing_payment_total.assert_any_call(payment, Decimal("108054"))

	def test_purchase_credit_note_reduces_vendor_payment_allocation(self):
		invoice = {"LineNum": 0, "InvoiceType": "it_PurchaseInvoice", "SumApplied": 42327.2}
		credit_note = {"LineNum": 1, "DocEntry": 112, "InvoiceType": "it_PurchaseCreditNote", "SumApplied": 1300}
		advice = {"LineNum": 2, "InvoiceType": "it_PaymentAdvice", "SumApplied": -1300}
		payment = {
			"DocEntry": 1881, "DocType": "rSupplier", "CardCode": "V00460",
			"DocCurrency": "SAR", "TransferSum": 39727.2,
			"PaymentInvoices": [invoice, credit_note, advice],
		}
		resolver = Mock()
		resolver.purchase_credit_note_journal.return_value = "5639"
		context = {"source": payment, "currency": "SAR", "sap_references": resolver}
		payment_rule = {"transform_config": {"source_kind": "payment", "currency_kind": "company"}}
		allocation_rule = {"transform_config": {"source_kind": "allocation", "currency_kind": "company"}}
		self.assertEqual(payment_ledger_amount(payment_rule, context), 0)
		line = {**{f"__parent_{key}": value for key, value in payment.items()}, **credit_note}
		self.assertEqual(payment_ledger_amount(allocation_rule, {**context, "source": line}), 1300)
		resolver.purchase_credit_note_journal.assert_any_call(112, "V00460", Decimal("1300"))


class TestPaymentRounding(FrappeTestCase):
	context = {"currency": "SAR"}

	def _payment(self, applied, transfer):
		return {
			"DocCurrency": "SAR", "TransferSum": transfer,
			"PaymentInvoices": [{"LineNum": i, "SumApplied": amount} for i, amount in enumerate(applied)],
		}

	def test_small_overage_is_taken_from_last_allocation(self):
		from emkan_insights.sap.services.transforms import _payment_allocation_amount, _payment_unallocated_amount

		payment = self._payment([527257.42, 244125.0], 771381.86)
		amounts = [
			_payment_allocation_amount(row, payment, "company", self.context)
			for row in payment["PaymentInvoices"]
		]
		self.assertEqual(sum(amounts), Decimal("771381.86"))
		self.assertEqual(amounts[0], Decimal("527257.42"))
		self.assertEqual(_payment_unallocated_amount(payment, "company", self.context), 0)

	def test_large_overage_still_fails(self):
		from emkan_insights.sap.services.transforms import _payment_unallocated_amount

		with self.assertRaises(frappe.ValidationError):
			_payment_unallocated_amount(self._payment([200.0], 100.0), "company", self.context)


class TestChequeTotals(FrappeTestCase):
	def test_cheque_lines_become_header_total_and_account(self):
		from emkan_insights.sap.services.fetch import _with_cheque_totals
		from emkan_insights.sap.services.transforms import _payment_method_account_code

		record = {"DocCurrency": "SAR", "CashSum": 0, "TransferSum": 0, "PaymentChecks": [
			{"CheckSum": 50000.0, "CheckAccount": "1113101", "Currency": "SAR"},
			{"CheckSum": 25000.0, "CheckAccount": "1113101", "Currency": "SAR"},
		]}
		_with_cheque_totals([record])
		self.assertEqual(record["CheckSum"], 75000.0)
		self.assertEqual(_payment_method_account_code(record), "1113101")


	def test_cancelled_payment_may_apply_more_than_it_paid(self):
		line = {"LineNum": 0, "DocEntry": 14955, "InvoiceType": "it_JournalEntry", "SumApplied": 171867.05}
		payment = {
			"DocEntry": 2389, "DocType": "rSupplier", "DocCurrency": "SAR",
			"TransferSum": 166711.04, "Cancelled": "tYES", "PaymentInvoices": [line],
		}
		context = {"source": payment, "currency": "SAR", "sap_references": Mock()}
		rule = {"transform_config": {"source_kind": "payment", "currency_kind": "company"}}
		self.assertEqual(payment_ledger_amount(rule, context), 0)
		payment["Cancelled"] = "tNO"
		with self.assertRaisesRegex(frappe.ValidationError, "applies"):
			payment_ledger_amount(rule, context)
