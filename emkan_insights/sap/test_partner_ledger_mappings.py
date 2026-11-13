import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS
from emkan_insights.sap.services import _sap_record_id
from emkan_insights.sap.services.fetch import _flatten_records, fetch
from emkan_insights.sap.services.mapper import Mapper


class TestPartnerLedgerMappings(FrappeTestCase):
	def test_gl_fetch_excludes_zero_amount_lines(self):
		client = Mock()
		client.fetch_all.return_value = [{
			"JdtNum": 91,
			"JournalEntryLines": [
				{"Line_ID": 0, "Debit": 25, "Credit": 0},
				{"Line_ID": 1, "Debit": 0, "Credit": 0},
				{"Line_ID": 2, "Debit": 0, "Credit": 25},
			],
		}]
		mapping = SimpleNamespace(
			name="SAP GL Journal Detail", sap_doctype="JournalEntries",
			sap_filter=None, full_snapshot_sync=True,
			fetch_limit=0, source_array_field="JournalEntryLines", sap_id_field="JdtNum+Line_ID",
		)

		rows = fetch(client, mapping)

		self.assertEqual([row["Line_ID"] for row in rows], [0, 2])

	def test_gl_lines_keep_header_fields_and_have_distinct_ids(self):
		definition = REVIEW_MAPPINGS["SAP GL Journal Detail"]
		rows = _flatten_records([{
			"JdtNum": 91,
			"ReferenceDate": "2026-10-01",
			"OriginalJournal": "ttARInvoice",
			"BaseReference": "INV-9",
			"JournalEntryLines": [
				{"Line_ID": 0, "AccountCode": "1000", "Debit": 25, "Credit": 0, "FCDebit": 0},
				{"Line_ID": 1, "AccountCode": "2000", "Debit": 0, "Credit": 25},
			],
		}], definition["source_array"], include_parent=True)
		self.assertEqual([_sap_record_id(row, definition["sap_id_field"]) for row in rows], ["91:0", "91:1"])
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value") as get_value:
			get_value.side_effect = lambda doctype, filters, *args, **kwargs: {
				"Account": "Cash - EX",
				"Journal Entry": "S-91",
			}.get(doctype)
			mapped = self._map("GL Entry", definition, rows[0])
		self.assertEqual((mapped["posting_date"], mapped["voucher_type"], mapped["voucher_no"]),
			("2026-10-01", "Journal Entry", "S-91"))
		self.assertEqual(mapped["voucher_subtype"], "ttARInvoice")
		self.assertEqual(mapped["debit"], 25)
		self.assertEqual(mapped["debit_in_account_currency"], 25)
		self.assertEqual(mapped["account_currency"], "SAR")

	def test_gl_line_points_to_imported_original_document(self):
		definition = REVIEW_MAPPINGS["SAP GL Journal Detail"]
		rows = _flatten_records([{
			"JdtNum": 92, "ReferenceDate": "2026-10-01",
			"OriginalJournal": "ttGeneralReleaseFromStock", "Original": 51212,
			"JournalEntryLines": [{"Line_ID": 0, "AccountCode": "1000", "Debit": 25, "Credit": 0, "FCDebit": 0}],
		}], definition["source_array"], include_parent=True)
		transforms = "emkan_insights.sap.services.transforms"
		with patch(f"{transforms}.frappe.db.get_value") as get_value, \
				patch(f"{transforms}.frappe.db.exists") as exists:
			get_value.side_effect = lambda doctype, filters, *args, **kwargs: {
				"Account": "Cash - EX", "Journal Entry": "S-92",
			}.get(doctype)
			exists.return_value = True
			mapped = self._map("GL Entry", definition, rows[0])
			self.assertEqual((mapped["voucher_type"], mapped["voucher_no"]), ("Stock Entry", "issue:51212"))
			exists.return_value = False
			transforms_module = __import__(transforms, fromlist=["_imported_document"])
			transforms_module._imported_document.cache_clear()
			mapped = self._map("GL Entry", definition, rows[0])
			self.assertEqual((mapped["voucher_type"], mapped["voucher_no"]), ("Journal Entry", "S-92"))

	def test_stock_issue_is_negative_and_uses_parent_voucher(self):
		definition = REVIEW_MAPPINGS["SAP Stock Goods Issue Detail"]
		row = _flatten_records([{
			"DocEntry": 8, "DocNum": 108, "DocDate": "2026-10-02",
			"DocumentLines": [{"LineNum": 0, "ItemCode": "ITEM-1", "WarehouseCode": "WH-1", "Quantity": 3}],
		}], definition["source_array"], include_parent=True)[0]
		mapped = self._map("Stock Ledger Entry", definition, row)
		self.assertEqual((mapped["actual_qty"], mapped["voucher_no"], mapped["item_code"]), (-3, "issue:8", "ITEM-1"))

	def test_stock_receipt_ledger_uses_stock_entry_voucher(self):
		definition = REVIEW_MAPPINGS["SAP Stock Goods Receipt Detail"]
		row = _flatten_records([{
			"DocEntry": 65, "DocNum": 26000000, "DocDate": "2026-10-21",
			"DocumentLines": [{"LineNum": 0, "ItemCode": "ITEM-1", "WarehouseCode": "WH-1", "Quantity": 2}],
		}], definition["source_array"], include_parent=True)[0]
		mapped = self._map("Stock Ledger Entry", definition, row)
		self.assertEqual((mapped["actual_qty"], mapped["voucher_no"]), (2, "receipt:65"))

	def test_stock_entries_do_not_treat_submitted_no_as_draft(self):
		path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
		catalog = {entry["name"]: entry for entry in json.loads(path.read_text())}
		for name in ("SAP Stock Entry Receipt", "SAP Stock Entry Issue"):
			mapping = SimpleNamespace(
				erp_doctype="Stock Entry", field_mapping={"docstatus": catalog[name]["field_mapping"]["docstatus"]},
			)
			mapper = Mapper(mapping)
			self.assertEqual(mapper.map({"Cancelled": "tNO", "Submitted": "tNO", "DocumentStatus": "bost_Open"})["docstatus"], 1)
			self.assertEqual(mapper.map({"Cancelled": "tYES", "Submitted": "tNO", "DocumentStatus": "bost_Close"})["docstatus"], 2)

	def test_payment_allocation_uses_child_invoice_and_parent_payment(self):
		definition = REVIEW_MAPPINGS["SAP Payment Incoming Allocation Detail"]
		row = _flatten_records([{
			"DocEntry": 12, "DocNum": 112, "DocDate": "2026-10-03",
			"DocType": "rCustomer", "CardCode": "C-1", "ControlAccount": "1100",
			"DocCurrency": "SAR", "TransferSum": 50, "CashSum": 0,
			"CheckSum": 0, "CreditSum": 0,
			"PaymentInvoices": [{"DocEntry": 30, "InvoiceType": "it_Invoice", "SumApplied": 50}],
		}], definition["source_array"], include_parent=True)[0]
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value") as get_value:
			get_value.side_effect = lambda doctype, filters, field="name": {
				"Account": "SAR" if field == "account_currency" else "Receivable - EX",
				"Customer": "C-1",
				"External Payment Entry": "PAYMENT-12",
				"External Sales Invoice": "INVOICE-30",
			}.get(doctype)
			mapped = self._map("Payment Ledger Entry", definition, row)
		self.assertEqual(_sap_record_id(row, definition["sap_id_field"]), "12:0")
		self.assertEqual((mapped["voucher_no"], mapped["against_voucher_no"], mapped["amount"]),
			("PAYMENT-12", "INVOICE-30", -50))
		self.assertEqual(mapped["account_type"], "Receivable")

	def test_payment_advice_links_to_staged_vendor_payment(self):
		definition = REVIEW_MAPPINGS["SAP Payment Outgoing Allocation Detail"]
		row = _flatten_records([{
			"DocEntry": 675, "DocDate": "2025-07-29", "CardCode": "V0462",
			"ControlAccount": "2131101", "DocCurrency": "SAR",
			"TransferSum": 11500, "CashSum": 0, "CheckSum": 0, "CreditSum": 0,
			"PaymentInvoices": [{
				"DocEntry": 1782, "InvoiceType": "it_PaymentAdvice", "SumApplied": -11500,
			}],
		}], definition["source_array"], include_parent=True)[0]
		resolver = Mock()
		resolver.vendor_payment_from_advice.return_value = "649"
		mapping = SimpleNamespace(erp_doctype="Payment Ledger Entry", field_mapping=definition["field_mapping"])
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value") as get_value:
			get_value.side_effect = lambda doctype, filters, field="name": {
				"Account": "SAR" if field == "account_currency" else "Payables - EX",
				"Supplier": "V0462",
				"External Payment Entry": (
				"PAYMENT-649" if filters.get("remote_id") == "outgoing:649" else "PAYMENT-675"
			),
			}.get(doctype)
			with patch.object(Mapper, "fill_item_uoms"):
				mapped = Mapper(mapping, context={
					"company": "Example", "currency": "SAR", "configuration_name": "Example",
					"sap_references": resolver,
				}).map(row)
		self.assertEqual(mapped["against_voucher_type"], "External Payment Entry")
		self.assertEqual((mapped["voucher_no"], mapped["against_voucher_no"], mapped["amount"]),
			("PAYMENT-675", "PAYMENT-649", 11500))
		resolver.vendor_payment_from_advice.assert_called_once_with(1782, "V0462")

	@staticmethod
	def _map(doctype, definition, row):
		mapping = SimpleNamespace(erp_doctype=doctype, field_mapping=definition["field_mapping"])
		with patch.object(Mapper, "fill_item_uoms"):
			return Mapper(mapping, context={"company": "Example", "currency": "SAR", "configuration_name": "Example"}).map(row)
