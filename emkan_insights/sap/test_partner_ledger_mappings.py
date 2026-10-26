from types import SimpleNamespace

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS
from emkan_insights.sap.services import _sap_record_id
from emkan_insights.sap.services.fetch import _flatten_records
from emkan_insights.sap.services.mapper import Mapper


class TestPartnerLedgerMappings(FrappeTestCase):
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
		mapped = self._map("GL Entry", definition, rows[0])
		self.assertEqual((mapped["posting_date"], mapped["voucher_type"], mapped["voucher_no"]),
			("2026-10-01", "ttARInvoice", "INV-9"))
		self.assertEqual(mapped["debit"], 25)
		self.assertEqual(mapped["debit_in_account_currency"], 25)
		self.assertEqual(mapped["account_currency"], "SAR")

	def test_stock_issue_is_negative_and_uses_parent_voucher(self):
		definition = REVIEW_MAPPINGS["SAP Stock Goods Issue Detail"]
		row = _flatten_records([{
			"DocEntry": 8, "DocNum": 108, "DocDate": "2026-10-02",
			"DocumentLines": [{"LineNum": 0, "ItemCode": "ITEM-1", "WarehouseCode": "WH-1", "Quantity": 3}],
		}], definition["source_array"], include_parent=True)[0]
		mapped = self._map("Stock Ledger Entry", definition, row)
		self.assertEqual((mapped["actual_qty"], mapped["voucher_no"], mapped["item_code"]), (-3, 108, "ITEM-1"))

	def test_payment_allocation_uses_child_invoice_and_parent_payment(self):
		definition = REVIEW_MAPPINGS["SAP Payment Incoming Allocation Detail"]
		row = _flatten_records([{
			"DocEntry": 12, "DocNum": 112, "DocDate": "2026-10-03",
			"DocType": "rCustomer", "CardCode": "C-1", "ControlAccount": "1100",
			"PaymentInvoices": [{"DocEntry": 30, "InvoiceType": "it_Invoice", "SumApplied": 50}],
		}], definition["source_array"], include_parent=True)[0]
		mapped = self._map("Payment Ledger Entry", definition, row)
		self.assertEqual(_sap_record_id(row, definition["sap_id_field"]), "12:0")
		self.assertEqual((mapped["voucher_no"], mapped["against_voucher_no"], mapped["amount"]), (112, 30, 50))
		self.assertEqual(mapped["account_type"], "Receivable")

	@staticmethod
	def _map(doctype, definition, row):
		mapping = SimpleNamespace(erp_doctype=doctype, field_mapping=definition["field_mapping"])
		return Mapper(mapping, context={"company": "Example", "currency": "SAR"}).map(row)
