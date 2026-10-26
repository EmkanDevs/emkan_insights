from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.balance_reconciliation import _comparison, _sap_balances, compare_ledgers


class TestBalanceReconciliation(FrappeTestCase):
	def test_sap_pagination_includes_manual_and_document_journals(self):
		client = Mock()
		client.get.side_effect = [
			{"value": [{"JdtNum": 1, "OriginalJournal": "ttJournalEntry", "JournalEntryLines": [
				{"AccountCode": "1000", "Debit": 10, "Credit": 0}
			]}], "@odata.nextLink": "JournalEntries?$skip=1"},
			{"value": [{"JdtNum": 2, "OriginalJournal": "ttARInvoice", "JournalEntryLines": [
				{"AccountCode": "1000", "Debit": 0, "Credit": "2.5"}
			]}]},
		]
		balances, count, journal_ids = _sap_balances(client, "2026-10-26")
		self.assertEqual(count, 2)
		self.assertEqual(journal_ids, {1, 2})
		self.assertEqual(balances["1000"], [Decimal("10"), Decimal("2.5")])
		self.assertEqual(client.get.call_count, 2)

	def test_reports_unmatched_and_balance_sheet_difference(self):
		accounts = {"1000": SimpleNamespace(name="Cash - EMK", root_type="Asset")}
		result = _comparison(
			"Emkan", "2026-10-26", accounts,
			{"1000": [Decimal("10"), Decimal("2")], "9999": [Decimal("1"), Decimal(0)]},
			{"Cash - EMK": [Decimal("7"), Decimal("2")], "Orphan": [Decimal("1"), Decimal(0)]},
			3,
		)
		self.assertFalse(result["matched"])
		self.assertEqual(result["unmatched_sap_account_numbers"], ["9999"])
		self.assertEqual(result["unmatched_sap_account_balances"][0]["balance"], "1")
		self.assertEqual(result["unmatched_erp_account_names"], ["Orphan"])
		self.assertEqual(result["balance_sheet_account_differences"][0]["difference"], "3")

	def test_rejects_incomplete_sap_journal(self):
		client = Mock()
		client.get.return_value = {"value": [{"JdtNum": 1, "JournalEntryLines": None}]}
		with self.assertRaises(frappe.ValidationError):
			_sap_balances(client, "2026-10-26")

	@patch("emkan_insights.sap.balance_reconciliation._erp_balances")
	@patch("emkan_insights.sap.balance_reconciliation._sap_journal_ids", return_value=({2}, 1))
	@patch("emkan_insights.sap.balance_reconciliation._sap_balances", return_value=({}, 1, {1}))
	@patch("emkan_insights.sap.balance_reconciliation._sap_client")
	@patch("emkan_insights.sap.balance_reconciliation.frappe.get_all", return_value=[])
	@patch("emkan_insights.sap.balance_reconciliation.frappe.get_doc")
	def test_comparison_rejects_mismatched_sap_journal_pages(
		self, get_doc, _get_all, _client, _balances, _ids, erp_balances
	):
		get_doc.return_value = SimpleNamespace(company="Emkan")
		with self.assertRaisesRegex(frappe.ValidationError, "different journal IDs"):
			compare_ledgers("Emkan", "2026-10-26")
		erp_balances.assert_not_called()
