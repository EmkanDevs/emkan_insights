# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services import _mark_completed
from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.services.fetch import _flatten_records, _query_params
from emkan_insights.sap.services.mapper import Mapper, normalize_mapping
from emkan_insights.sap.services.staging import StagingWriter


class TestSAPDoctypeMapping(FrappeTestCase):
	def test_maps_parent_and_child_fields(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Sales Order",
			field_mapping={
				"customer": "CardCode",
				"transaction_date": {"fieldname": "DocDate", "transform": "sap_date"},
				"items": {
					"fieldname": "DocumentLines",
					"mapping": [
						{"item_code": "ItemCode"},
						{"qty": "Quantity"},
					],
				},
			},
		)

		result = Mapper(mapping_doc).map(
			{
				"CardCode": "C-001",
				"DocDate": "2026-09-16T00:00:00Z",
				"DocumentLines": [{"ItemCode": "ITEM-1", "Quantity": 2}],
			}
		)

		self.assertEqual(result["doctype"], "Sales Order")
		self.assertEqual(result["customer"], "C-001")
		self.assertEqual(result["transaction_date"], "2026-09-16")
		self.assertEqual(result["items"], [{"item_code": "ITEM-1", "qty": 2}])

	def test_rejects_duplicate_fields_in_mapping_array(self):
		with self.assertRaises(frappe.ValidationError):
			normalize_mapping([{"item_code": "ItemCode"}, {"item_code": "Code"}])

	def test_rejects_non_array_child_data(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Sales Order",
			field_mapping={
				"items": {
					"fieldname": "DocumentLines",
					"mapping": [{"item_code": "ItemCode"}],
				}
			},
		)

		with self.assertRaises(frappe.ValidationError):
			Mapper(mapping_doc).map({"DocumentLines": {"ItemCode": "ITEM-1"}})

	def test_inverts_sap_boolean(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="UOM",
			field_mapping={
				"must_be_whole_number": {
					"fieldname": "AllowDecimalLength",
					"transform": "sap_bool_not",
				}
			},
		)

		mapper = Mapper(mapping_doc)

		self.assertEqual(mapper.map({"AllowDecimalLength": "tYES"})["must_be_whole_number"], 0)
		self.assertEqual(mapper.map({"AllowDecimalLength": "tNO"})["must_be_whole_number"], 1)

	def test_maps_sap_y_n_boolean(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Item Group",
			field_mapping={"is_group": {"fieldname": "U_Type", "transform": "sap_bool"}},
		)

		mapper = Mapper(mapping_doc)

		self.assertEqual(mapper.map({"U_Type": "Y"})["is_group"], 1)
		self.assertEqual(mapper.map({"U_Type": "N"})["is_group"], 0)

	def test_maps_configured_values(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Employee",
			field_mapping={
				"status": {
					"fieldname": "Active",
					"transform": "value_map",
					"transform_config": {"mapping": {"tYES": "Active"}, "default": "Inactive"},
				}
			},
		)

		mapper = Mapper(mapping_doc)

		self.assertEqual(mapper.map({"Active": "tYES"})["status"], "Active")
		self.assertEqual(mapper.map({"Active": "tNO"})["status"], "Inactive")

	def test_maps_value_from_multiple_source_fields(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Payment Term",
			field_mapping={
				"due_date_based_on": {
					"fieldname": "StartFrom",
					"transform": "conditional_value",
					"transform_config": {
						"conditions": [
							{
								"when": {
									"StartFrom": "pdt_None",
									"NumberOfAdditionalMonths": 0,
									"NumberOfInstallments": [None, 0, 1],
								},
								"value": "Day(s) after invoice date",
							}
						],
						"strict": True,
					},
				}
			},
		)
		mapper = Mapper(mapping_doc)

		result = mapper.map(
			{"StartFrom": "pdt_None", "NumberOfAdditionalMonths": 0, "NumberOfInstallments": 1}
		)

		self.assertEqual(result["due_date_based_on"], "Day(s) after invoice date")

	def test_rejects_unrepresentable_conditional_value(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Payment Term",
			field_mapping={
				"due_date_based_on": {
					"fieldname": "StartFrom",
					"transform": "conditional_value",
					"transform_config": {
						"conditions": [
							{
								"when": {"StartFrom": "pdt_None", "NumberOfAdditionalMonths": 0},
								"value": "Day(s) after invoice date",
							}
						],
						"strict": True,
					},
				}
			},
		)

		with self.assertRaises(frappe.ValidationError):
			Mapper(mapping_doc).map({"StartFrom": "pdt_None", "NumberOfAdditionalMonths": 2})

	def test_conditional_value_supports_prefix_rules(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Account",
			field_mapping={
				"root_type": {
					"fieldname": "Code",
					"transform": "conditional_value",
					"transform_config": {
						"conditions": [
							{"when": {"Code": {"starts_with": "1"}}, "value": "Asset"}
						],
						"strict": True,
					},
				}
			},
		)

		self.assertEqual(Mapper(mapping_doc).map({"Code": "1000"})["root_type"], "Asset")

	def test_maps_first_nonblank_source_value(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Lead",
			field_mapping={
				"first_name": {
					"transform": "first_value",
					"transform_config": {"fields": ["ContactPerson", "CardName"]},
				}
			},
		)

		self.assertEqual(
			Mapper(mapping_doc).map({"ContactPerson": "", "CardName": "Example"})["first_name"],
			"Example",
		)

	def test_maps_configuration_context(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Project",
			field_mapping={
				"company": {
					"transform": "context",
					"transform_config": {"key": "company"},
				}
			},
		)

		result = Mapper(mapping_doc, context={"company": "SAP Company"}).map({})

		self.assertEqual(result["company"], "SAP Company")

	def test_combines_static_and_date_filters(self):
		mapping_doc = SimpleNamespace(
			sap_filter="CardType eq 'cCustomer'",
			date_filter_field="CreateDate",
		)

		params = _query_params(mapping_doc, "2026-01-01", "2026-01-31")

		self.assertEqual(
			params["$filter"],
			"(CardType eq 'cCustomer') and (CreateDate ge '2026-01-01' and CreateDate le '2026-01-31')",
		)

	def test_flattens_nested_master_rows(self):
		records = [{"CardCode": "C-1", "ContactEmployees": [{"InternalCode": 10}, {"InternalCode": 11}]}]

		self.assertEqual(
			_flatten_records(records, "ContactEmployees"),
			[{"InternalCode": 10}, {"InternalCode": 11}],
		)

	def test_initial_load_does_not_apply_incremental_filter(self):
		mapping_doc = SimpleNamespace(sap_filter=None, date_filter_field="UpdateDate")

		self.assertIsNone(_query_params(mapping_doc, None, None))

	def test_uses_changed_mapping_without_a_code_registry(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Item", field_mapping='{"item_name": "ItemName"}'
		)
		record = {"ItemName": "Original", "ForeignName": "Alternative"}

		self.assertEqual(Mapper(mapping_doc).map(record)["item_name"], "Original")
		mapping_doc.field_mapping = '{"item_name": "ForeignName"}'
		self.assertEqual(Mapper(mapping_doc).map(record)["item_name"], "Alternative")


class TestSAPB1Client(FrappeTestCase):
	def test_fetch_all_follows_odata_next_link(self):
		client = SAPB1Client("https://sap.example.com", "COMPANY", "user", "secret")
		client.get = Mock(
			side_effect=[
				{"value": [{"DocEntry": 1}], "@odata.nextLink": "Orders?$skip=1"},
				{"value": [{"DocEntry": 2}]},
			]
		)

		result = client.fetch_all("Orders", {"$filter": "DocDate ge '2026-01-01'"})

		self.assertEqual(result, [{"DocEntry": 1}, {"DocEntry": 2}])
		self.assertEqual(client.get.call_count, 2)
		client.get.assert_any_call("Orders?$skip=1", params=None)


class TestSAPSyncCursor(FrappeTestCase):
	@patch("emkan_insights.sap.services.now_datetime", return_value="2026-09-17 11:01:00")
	@patch("emkan_insights.sap.services.frappe")
	def test_advances_cursor_only_after_successful_automatic_window(self, frappe_mock, _now):
		row = SimpleNamespace(name="ROW-1")
		result = {"failed": []}

		_mark_completed(row, "2026-09-17 11:00:00", result, automatic_window=True)

		values = frappe_mock.db.set_value.call_args.args[2]
		self.assertEqual(values["last_sync"], "2026-09-17 11:00:00")
		self.assertEqual(result["last_sync"], "2026-09-17 11:00:00")

	@patch("emkan_insights.sap.services.now_datetime", return_value="2026-09-17 11:01:00")
	@patch("emkan_insights.sap.services.frappe")
	def test_does_not_advance_cursor_when_a_record_fails(self, frappe_mock, _now):
		result = {"failed": ["ITEM-1"]}

		_mark_completed(
			SimpleNamespace(name="ROW-1"),
			"2026-09-17 11:00:00",
			result,
			automatic_window=True,
		)

		values = frappe_mock.db.set_value.call_args.args[2]
		self.assertNotIn("last_sync", values)
		self.assertIsNone(result["last_sync"])


class TestStagingWriter(FrappeTestCase):
	@patch("emkan_insights.sap.services.staging.frappe")
	def test_creates_source_scoped_staging_record(self, frappe_mock):
		staging_doc = Mock()
		staging_doc.name = "STAGE-1"
		frappe_mock.db.get_value.return_value = None
		frappe_mock.as_json.return_value = '{"doctype":"UOM"}'
		frappe_mock.get_doc.return_value = staging_doc

		action, name = StagingWriter("External Uom", "SAP Esacon").upsert(
			"KG", {"doctype": "UOM"}
		)

		self.assertEqual((action, name), ("created", "STAGE-1"))
		frappe_mock.db.get_value.assert_called_once_with(
			"External Uom",
			{
				"external_sap_configuration": "SAP Esacon",
				"remote_id": "KG",
			},
			"name",
		)
		values = frappe_mock.get_doc.call_args.args[0]
		self.assertEqual(values["source_site"], None)
		self.assertEqual(values["external_sap_configuration"], "SAP Esacon")
		self.assertIsNone(values["last_synced_on"])
		staging_doc.insert.assert_called_once_with(ignore_permissions=True)

	@patch("emkan_insights.sap.services.staging.frappe")
	def test_updates_existing_staging_record(self, frappe_mock):
		staging_doc = Mock()
		staging_doc.name = "STAGE-1"
		frappe_mock.db.get_value.return_value = "STAGE-1"
		frappe_mock.get_doc.return_value = staging_doc

		action, name = StagingWriter("External Uom", "SAP Esacon").upsert(
			"KG", {"doctype": "UOM"}
		)

		self.assertEqual((action, name), ("updated", "STAGE-1"))
		self.assertNotIn("last_synced_on", staging_doc.update.call_args.args[0])
		staging_doc.save.assert_called_once_with(ignore_permissions=True)
