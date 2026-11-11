import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.services import _account_party_types
from emkan_insights.sap.services.transforms import apply_transform


class TestSAPAccountBranchMapping(FrappeTestCase):
	def test_party_control_accounts_are_classified_from_source(self):
		rule = self._account_fields()["account_type"]
		party_types = {"customer-account": "Receivable", "supplier-account": "Payable"}
		for code, expected in party_types.items():
			with self.subTest(code=code):
				self.assertEqual(self._map(
					rule, {"Code": code, "AccountType": "at_Other"},
					company="Example", account_party_types=party_types,
				), expected)
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value="Temporary"):
			self.assertEqual(self._map(
				rule, {"Code": "manual-account", "AccountType": "at_Other"}, company="Example",
			), "Temporary")

	def test_partner_control_accounts_reject_conflicting_roles(self):
		client = Mock()
		client.fetch_all.return_value = [
			{"CardType": "cCustomer", "DebitorAccount": "shared"},
			{"CardType": "cSupplier", "DebitorAccount": "shared"},
		]
		with self.assertRaisesRegex(frappe.ValidationError, "both customers and suppliers"):
			_account_party_types(client)

	def test_level_one_names_map_to_root_and_report_types(self):
		fields = self._account_fields()
		for name, expected_root, expected_report in (
			("Assets", "Asset", "Balance Sheet"),
			("Liabilities", "Liability", "Balance Sheet"),
			("Equity", "Equity", "Balance Sheet"),
			("Capital and Reserves", "Equity", "Balance Sheet"),
			("Revenues", "Income", "Profit and Loss"),
			("Turnover", "Income", "Profit and Loss"),
			("Cost of Sales", "Expense", "Profit and Loss"),
			("Expenses", "Expense", "Profit and Loss"),
			("Operating Costs", "Expense", "Profit and Loss"),
			("Non-Operating Income and Expenditure", "Expense", "Profit and Loss"),
			("Taxation and Extraordinary Items", "Expense", "Profit and Loss"),
			("Transition Account", "Expense", "Profit and Loss"),
			("Transition", "Asset", "Balance Sheet"),
			("#10", "Equity", "Balance Sheet"),
		):
			with self.subTest(name=name):
				source = {"Code": name, "AccountLevel": 1, "Name": f" {name} "}
				root_type = self._map(fields["root_type"], source)
				self.assertEqual(root_type, expected_root)
				self.assertEqual(
					self._map(fields["report_type"], source, {"root_type": root_type}),
					expected_report,
				)

	def test_descendants_inherit_their_level_one_ancestor(self):
		fields = self._account_fields()
		parents = {
			"100000000000000": None, "1100000": "100000000000000",
			"1110000": "1100000", "200000000000000": None,
			"2100000": "200000000000000",
		}
		names = {"100000000000000": "Assets", "200000000000000": "Liabilities"}
		for code, level, expected_root in (
			("1100000", 2, "Asset"),
			("2100000", 2, "Liability"),
			("1110000", 3, "Asset"),
		):
			with self.subTest(code=code):
				source = {"Code": code, "AccountLevel": level, "Name": "Ignored"}
				root_type = self._map(fields["root_type"], source,
					account_parents=parents, account_names=names)
				self.assertEqual(root_type, expected_root)
				self.assertEqual(
					self._map(fields["report_type"], source, {"root_type": root_type}),
					"Balance Sheet",
				)

	def test_previously_promoted_branches_inherit_their_level_one_root(self):
		rule = self._account_fields()["root_type"]
		parents = {
			"700000000000000": None, "7200000": "700000000000000",
			"7210000": "7200000", "7800000": "700000000000000",
			"7810000": "7800000", "900000000000000": None,
			"911000": "900000000000000", "911400": "911000",
		}
		names = {"700000000000000": "Non-Operating Income and Expenditure",
			"900000000000000": "Transition"}
		for code, expected_root in (
			("7210000", "Expense"), ("7810000", "Expense"),
			("911400", "Asset"), ("911000", "Asset"),
		):
			with self.subTest(code=code):
				self.assertEqual(self._map(
					rule, {"Code": code, "AccountLevel": 3, "Name": "Ignored"},
					account_parents=parents, account_names=names,
				), expected_root)

	def test_unknown_account_level_or_name_is_rejected(self):
		rule = self._account_fields()["root_type"]
		for source in (
			{"Code": "unknown", "AccountLevel": 1, "Name": "Unknown"},
			{"Code": "unknown", "AccountLevel": 11, "Name": "Assets"},
			{"Code": "unknown", "AccountLevel": "invalid", "Name": "Assets"},
		):
			with self.subTest(source=source):
				with self.assertRaises(frappe.ValidationError):
					self._map(rule, source)

	def test_report_type_requires_a_classified_root(self):
		with self.assertRaisesRegex(frappe.ValidationError, "has no ERPNext report type"):
			self._map(self._account_fields()["report_type"], {"Code": "unknown"})

	def test_report_type_follows_mapped_root_type(self):
		rule = self._account_fields()["report_type"]
		for root_type, expected in (
			("Asset", "Balance Sheet"), ("Liability", "Balance Sheet"),
			("Equity", "Balance Sheet"), ("Income", "Profit and Loss"),
			("Expense", "Profit and Loss"),
		):
			with self.subTest(root_type=root_type):
				self.assertEqual(self._map(
					rule, {"AccountType": "at_Other"}, {"root_type": root_type},
				), expected)

	def test_mapper_uses_account_level_for_root_and_report_type(self):
		fields = self._account_fields()
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Account",
			field_mapping={key: fields[key] for key in ("root_type", "report_type")},
		))
		result = mapper.map({"Code": "new-revenue", "AccountLevel": 1, "Name": "Revenues"})
		self.assertEqual((result["root_type"], result["report_type"]), ("Income", "Profit and Loss"))
		self.assertNotIn("_sap_mapping_errors", result)

	@staticmethod
	def _account_fields():
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		return next(row["field_mapping"] for row in catalog if row["name"] == "SAP Account")

	@staticmethod
	def _map(rule, source, mapped_fields=None, company=None, account_parents=None, account_party_types=None,
		account_names=None):
		return apply_transform(
			source.get(rule.get("fieldname")), rule,
			{"source": source, "target_field": "root_type", "mapped_fields": mapped_fields,
			 "company": company, "account_parents": account_parents,
			 "account_names": account_names, "account_party_types": account_party_types},
		)
