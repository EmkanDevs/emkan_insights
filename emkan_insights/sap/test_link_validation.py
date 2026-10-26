from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.link_validation import resolve_account_links, validate_document_links


class TestSAPLinkValidation(FrappeTestCase):
	@patch("emkan_insights.sap.link_validation.frappe.get_all", return_value=["Cash - EMK"])
	@patch("emkan_insights.sap.link_validation.frappe.db.exists", return_value=False)
	@patch("emkan_insights.sap.link_validation.frappe.get_meta")
	def test_resolves_account_number_with_company(self, get_meta, _exists, get_all):
		get_meta.return_value.fields = [
			SimpleNamespace(fieldtype="Link", fieldname="account", options="Account")
		]
		documents = [{
			"Journal Entry": [{"name": "JE-1", "company": "Emkan"}],
			"Journal Entry Account": [{"name": "line-1", "account": "1000"}],
		}]
		resolve_account_links(documents)
		self.assertEqual(documents[0]["Journal Entry Account"][0]["account"], "Cash - EMK")
		get_all.assert_called_once_with(
			"Account", filters={"company": "Emkan", "account_number": "1000"},
			pluck="name", limit_page_length=2,
		)

	@patch("emkan_insights.sap.link_validation.frappe.get_all", return_value=[])
	@patch("emkan_insights.sap.link_validation.frappe.db.exists", return_value=False)
	@patch("emkan_insights.sap.link_validation.frappe.get_meta")
	def test_resolves_account_parent_created_in_same_batch(self, get_meta, _exists, _get_all):
		get_meta.return_value.fields = [
			SimpleNamespace(fieldtype="Link", fieldname="parent_account", options="Account")
		]
		documents = [{"Account": [
			{"name": "Root - EMK", "account_number": "1000", "company": "Emkan"},
			{"name": "Cash - EMK", "account_number": "1010", "company": "Emkan", "parent_account": "1000"},
		]}]
		resolve_account_links(documents)
		self.assertEqual(documents[0]["Account"][1]["parent_account"], "Root - EMK")

	@patch("emkan_insights.sap.link_validation.frappe.get_all", return_value=[])
	@patch("emkan_insights.sap.link_validation.frappe.get_meta")
	def test_rejects_missing_parent_and_child_links(self, get_meta, _get_all):
		def meta(doctype):
			fields = {
				"Account": [SimpleNamespace(fieldtype="Link", fieldname="parent_account", options="Account")],
				"Journal Entry Account": [SimpleNamespace(fieldtype="Link", fieldname="account", options="Account")],
			}
			return SimpleNamespace(fields=fields.get(doctype, []))

		get_meta.side_effect = meta
		with self.assertRaisesRegex(frappe.ValidationError, "Account link"):
			validate_document_links([{
				"Account": [{"name": "Cash - EMK", "parent_account": "1000"}],
				"Journal Entry Account": [{"name": "line-1", "account": "2000"}],
			}])

	@patch("emkan_insights.sap.link_validation.frappe.get_all", return_value=[])
	@patch("emkan_insights.sap.link_validation.frappe.get_meta")
	def test_accepts_parent_created_in_same_batch(self, get_meta, get_all):
		get_meta.return_value.fields = [
			SimpleNamespace(fieldtype="Link", fieldname="parent_account", options="Account")
		]
		validate_document_links([{"Account": [
			{"name": "Root", "parent_account": None},
			{"name": "Cash", "parent_account": "Root"},
		]}])
		get_all.assert_not_called()
