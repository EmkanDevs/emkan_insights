import json
from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.transforms import apply_transform


class TestSAPAccountBranchMapping(FrappeTestCase):
	def test_verified_nonoperating_branches(self):
		fields = self._account_fields()
		for code, expected in (
			("7100000", "Expense"), ("7300000", "Expense"),
			("7400000", "Expense"), ("7800000", "Income"),
			("7900000", "Expense"),
		):
			with self.subTest(code=code):
				source = {"Code": code, "AccountType": "at_Other"}
				self.assertEqual(self._map(fields["root_type"], source), expected)
				self.assertEqual(self._map(fields["report_type"], source), "Profit and Loss")

	def test_mixed_and_unclassified_roots_stay_unmapped(self):
		rule = self._account_fields()["root_type"]
		for code in ("700000000000000", "7200000", "900000000000000", "A00000000000000"):
			with self.subTest(code=code):
				with self.assertRaisesRegex(frappe.ValidationError, "No conditional_value match"):
					self._map(rule, {"Code": code, "AccountType": "at_Other"})

	def test_existing_profit_account_stays_equity(self):
		fields = self._account_fields()
		source = {"Code": "profit", "AccountType": "at_Other"}
		self.assertEqual(self._map(fields["root_type"], source), "Equity")
		self.assertEqual(self._map(fields["report_type"], source), "Balance Sheet")

	@staticmethod
	def _account_fields():
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		return next(row["field_mapping"] for row in catalog if row["name"] == "SAP Account")

	@staticmethod
	def _map(rule, source):
		return apply_transform(source.get(rule.get("fieldname")), rule, {"source": source, "target_field": "root_type"})
