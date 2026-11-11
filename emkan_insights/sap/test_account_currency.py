from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.transforms import apply_transform, lookup

ACCOUNT_CURRENCY_RULE = {
	"transform": "lookup",
	"transform_config": {
		"doctype": "Account",
		"field": "account_number",
		"return_field": "account_currency",
		"company_context": True,
		"required": True,
	},
}
CONTEXT = {"company": "Samif"}


class TestSAPAccountCurrency(FrappeTestCase):
	def test_all_currencies_marker_maps_to_blank(self):
		rule = {
			"fieldname": "AcctCurrency",
			"transform": "value_map",
			"transform_config": {"mapping": {"##": None}},
		}
		self.assertIsNone(apply_transform("##", rule))
		self.assertEqual(apply_transform("SAR", rule), "SAR")
		self.assertEqual(apply_transform("USD", rule), "USD")

	@patch("emkan_insights.sap.services.transforms.frappe.get_cached_value", return_value="SAR")
	@patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True)
	@patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=None)
	def test_blank_currency_account_falls_back_to_company_currency(self, *_mocks):
		self.assertEqual(lookup("2161102", ACCOUNT_CURRENCY_RULE, CONTEXT), "SAR")

	@patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True)
	@patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value="USD")
	def test_explicit_currency_is_preserved(self, *_mocks):
		self.assertEqual(lookup("2161102", ACCOUNT_CURRENCY_RULE, CONTEXT), "USD")

	@patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=False)
	@patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=None)
	def test_missing_account_still_fails(self, *_mocks):
		with self.assertRaises(frappe.ValidationError):
			lookup("99999999", ACCOUNT_CURRENCY_RULE, CONTEXT)
