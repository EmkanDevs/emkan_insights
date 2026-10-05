import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.patches.v0_0_1.remove_temporary_ledger_duplicates import (
	_matches_temporary_row,
	_target_name,
)


class TestRemoveTemporaryLedgerDuplicates(FrappeTestCase):
	def test_company_prefix_is_applied_once(self):
		context = frappe._dict(abbr="IMC", abbr_required=1)

		self.assertEqual(_target_name("ACC-GLE-2026-00001", context), "IMC-ACC-GLE-2026-00001")
		self.assertEqual(_target_name("IMC-ACC-GLE-2026-00001", context), "IMC-ACC-GLE-2026-00001")

	def test_matches_gl_entry_from_temporary_snapshot(self):
		candidate = frappe._dict(
			voucher_type="Sales Invoice",
			voucher_no="IMC-CINV-26-00204",
			voucher_detail_no=None,
			posting_date="2026-09-09",
			is_cancelled=0,
			account="1131101 - Accounts Receivable Local - IMC",
			debit=68375,
			credit=0,
		)
		temporary_row = {
			"context": frappe._dict(abbr="IMC"),
			"document": {
				"voucher_type": "Sales Invoice",
				"voucher_no": "IMC-CINV-26-00204",
				"voucher_detail_no": None,
				"posting_date": "2026-09-09",
				"is_cancelled": 0,
				"account": "1131101 - Accounts Receivable Local - IMC",
				"debit": 68375,
				"credit": 0,
			},
		}

		self.assertTrue(_matches_temporary_row("GL Entry", candidate, temporary_row))

		candidate.debit = 1
		self.assertFalse(_matches_temporary_row("GL Entry", candidate, temporary_row))
