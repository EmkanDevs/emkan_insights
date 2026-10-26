from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services import _process_record, get_mapped_data
from emkan_insights.sap.services.exclusions import (
	allowed_staged_id_condition,
	get_excluded_sap_id_prefixes,
	is_excluded_sap_id,
)


class TestSAPExclusions(FrappeTestCase):
	def test_parses_and_validates_prefixes(self):
		mapping = SimpleNamespace(excluded_sap_id_prefixes=" 7, 9, A ")
		prefixes = get_excluded_sap_id_prefixes(mapping)
		self.assertEqual(prefixes, ("7", "9", "A"))
		self.assertTrue(is_excluded_sap_id("911400", prefixes))
		self.assertFalse(is_excluded_sap_id("100000", prefixes))
		for invalid in ("7,7", "7,%"):
			with self.assertRaises(frappe.ValidationError):
				get_excluded_sap_id_prefixes(SimpleNamespace(excluded_sap_id_prefixes=invalid))

	def test_staged_id_condition_excludes_prefixed_rows(self):
		source = frappe.qb.DocType("External Account")
		condition = allowed_staged_id_condition(source.remote_id, "account:", ("7", "9", "A"))
		sql = str(condition)
		for prefix in ("account:7%", "account:9%", "account:A%"):
			self.assertIn(prefix, sql)
		self.assertEqual(sql.upper().count("NOT"), 3)

	@patch("emkan_insights.sap.services._mark_completed")
	@patch("emkan_insights.sap.services._mark_started")
	@patch("emkan_insights.sap.services._get_mapping_row", return_value=None)
	@patch("emkan_insights.sap.services._process_record")
	@patch("emkan_insights.sap.services.fetch")
	@patch("emkan_insights.sap.services.SAPB1Client.from_configuration")
	@patch("emkan_insights.sap.services.frappe.get_doc")
	@patch("emkan_insights.sap.services.frappe.db.get_value")
	def test_fetch_stages_excluded_account_for_review(
		self, get_value, get_doc, _client, fetch, process_record, _row, _started, _completed
	):
		mapping = SimpleNamespace(
			name="SAP Account", enabled=1, sap_id_field="Code", excluded_sap_id_prefixes="7,9,A",
			external_doctype="External Account", erp_doctype="Account",
			field_mapping={"account_name": "Name"},
		)
		configuration = SimpleNamespace(name="SAP - Company", company="Test Company")
		get_doc.side_effect = [mapping, configuration]
		get_value.return_value = "SAR"
		fetch.return_value = [{"Code": "911400"}, {"Code": "100000"}]

		result = get_mapped_data("SAP Account", configuration_name=configuration.name)

		self.assertEqual(result["excluded"], ["911400"])
		self.assertEqual(process_record.call_count, 2)
		self.assertEqual(process_record.call_args_list[0].args[3], {"Code": "911400"})
		self.assertEqual(
			process_record.call_args_list[0].kwargs["forced_hold_reason"],
			"SAP Account prefix needs accounting classification",
		)
		self.assertEqual(process_record.call_args_list[1].args[3], {"Code": "100000"})

	@patch("emkan_insights.sap.services.frappe.db.savepoint")
	def test_excluded_account_keeps_raw_source_and_hold(self, _savepoint):
		mapping = SimpleNamespace(name="SAP Account", sap_doctype="ChartOfAccounts", sap_id_field="Code")
		mapper = Mock()
		writer = Mock()
		writer.upsert.return_value = ("created", "EXT-1")
		result = {"success": [], "created": [], "updated": [], "unchanged": [], "failed": []}
		_process_record(
			mapping, mapper, writer, {"Code": "911400", "Name": "Review"}, 0, result,
			forced_hold_reason="SAP Account prefix needs accounting classification",
		)
		writer.upsert.assert_called_once_with(
			"911400",
			{"sap_mapping": "SAP Account", "source_endpoint": "ChartOfAccounts", "record": {"Code": "911400", "Name": "Review"}},
			sync_hold_reason="SAP Account prefix needs accounting classification",
		)
		mapper.map.assert_not_called()
		self.assertEqual(result["success"], ["911400"])

	@patch("emkan_insights.sap.services._mark_completed")
	@patch("emkan_insights.sap.services._get_mapping_row", return_value=None)
	@patch("emkan_insights.sap.services._process_record", side_effect=frappe.ValidationError("No root_type match"))
	@patch("emkan_insights.sap.services.fetch")
	@patch("emkan_insights.sap.services.SAPB1Client.from_configuration")
	@patch("emkan_insights.sap.services.frappe.get_doc")
	@patch("emkan_insights.sap.services.frappe.db.get_value")
	def test_fetch_stops_at_first_record_error(
		self, get_value, get_doc, _client, fetch, process_record, _row, mark_completed
	):
		mapping = SimpleNamespace(
			enabled=1, sap_id_field="Code", excluded_sap_id_prefixes="",
			external_doctype="External Account", erp_doctype="Account",
			field_mapping={"account_name": "Name"},
		)
		configuration = SimpleNamespace(name="SAP - Company", company="Test Company")
		get_doc.side_effect = [mapping, configuration]
		get_value.return_value = "SAR"
		fetch.return_value = [{"Code": "profit"}, {"Code": "100000"}]

		with self.assertRaisesRegex(frappe.ValidationError, "No root_type match"):
			get_mapped_data("SAP Account", configuration_name=configuration.name)

		process_record.assert_called_once()
		mark_completed.assert_not_called()
