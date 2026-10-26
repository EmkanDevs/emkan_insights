# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.patches.v0_0_1.add_missing_sap_mappings import MAPPING_NAMES
from emkan_insights.patches.v0_0_1 import put_sap_account_name_before_code
from emkan_insights.sap.services import _mark_completed, _process_record
from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.services.fetch import _flatten_records, _query_params
from emkan_insights.sap.services.mapper import Mapper, normalize_mapping
from emkan_insights.sap.services.staging import StagingWriter


class TestSAPDoctypeMapping(FrappeTestCase):
	def test_sap_document_status_maps_to_erpnext_docstatus(self):
		catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
		mapping = next(
			entry for entry in json.loads(catalog_path.read_text(encoding="utf-8"))
			if entry["name"] == "SAP Purchase Order"
		)
		doc = frappe.get_doc({"doctype": "SAP Doctype Mapping", **mapping})
		doc.validate()
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Purchase Order",
			field_mapping={"docstatus": mapping["field_mapping"]["docstatus"]},
		))
		self.assertEqual(mapper.map({"Cancelled": "tNO", "DocumentStatus": "bost_Open"})["docstatus"], 1)
		self.assertEqual(mapper.map({"Cancelled": "tNO", "DocumentStatus": "bost_Close"})["docstatus"], 1)
		self.assertEqual(mapper.map({"Cancelled": "tYES", "DocumentStatus": "bost_Close"})["docstatus"], 2)

	def test_account_name_combines_sap_name_and_code(self):
		catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
		mapping = next(
			entry for entry in json.loads(catalog_path.read_text(encoding="utf-8"))
			if entry["name"] == "SAP Account"
		)
		account_name_rule = mapping["field_mapping"]["account_name"]
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Account", field_mapping={"account_name": account_name_rule}
		))
		self.assertEqual(
			mapper.map({"Code": "700000000000000", "Name": "Non-Operating Income and Expenditure"})[
				"account_name"
			],
			"Non-Operating Income and Expenditure - 700000000000000",
		)
		self.assertEqual(mapper.map({"Code": "profit", "Name": None})["account_name"], "profit")
		with self.assertRaisesRegex(frappe.ValidationError, "at least one source value"):
			mapper.map({"Name": None})

	def test_account_name_join_requires_valid_source_fields(self):
		with self.assertRaises(frappe.ValidationError):
			normalize_mapping({
				"account_name": {"transform": "join_fields", "transform_config": {"fields": ["Code"]}}
			})

	def test_existing_account_mapping_puts_name_before_code(self):
		mapping = Mock(field_mapping=json.dumps({
			"account_name": {
				"transform": "join_fields",
				"transform_config": {
					"fields": ["Code", "Name"], "separator": " - ", "skip_missing": True
				},
			}
		}))
		with (
			patch.object(put_sap_account_name_before_code.frappe.db, "exists", return_value=True),
			patch.object(put_sap_account_name_before_code.frappe, "get_doc", return_value=mapping),
		):
			put_sap_account_name_before_code.execute()
		self.assertEqual(
			json.loads(mapping.field_mapping)["account_name"]["transform_config"]["fields"],
			["Name", "Code"],
		)
		mapping.save.assert_called_once_with(ignore_permissions=True)

	def test_candidate_bom_maps_required_fields_and_component_rows(self):
		catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
		mapping = next(
			entry for entry in json.loads(catalog_path.read_text(encoding="utf-8"))
			if entry["name"] == "SAP BOM"
		)
		with patch("emkan_insights.sap.services.mapper.frappe.get_all", return_value=[]):
			result = Mapper(
				SimpleNamespace(**mapping), context={"company": "Test Company", "currency": "SAR"}
			).map({
				"TreeCode": "FINISHED-1",
				"Quantity": 2,
				"ProductTreeLines": [
					{"ItemCode": "COMPONENT-1", "Quantity": 3, "InventoryUOM": "Nos", "Price": 5}
				],
			})
		self.assertEqual(result["company"], "Test Company")
		self.assertEqual(result["currency"], "SAR")
		self.assertEqual(result["conversion_rate"], 1)
		self.assertEqual(result["items"][0]["item_code"], "COMPONENT-1")
		self.assertEqual(result["items"][0]["qty"], 3)
		self.assertEqual(result["items"][0]["rate"], 5)
		self.assertFalse({field.fieldname for field in frappe.get_meta("BOM").fields if field.reqd} - result.keys())

	def test_payment_method_uses_code_when_description_is_missing(self):
		catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
		mapping = next(
			entry for entry in json.loads(catalog_path.read_text(encoding="utf-8"))
			if entry["name"] == "SAP Mode of Payment"
		)
		result = Mapper(SimpleNamespace(**mapping)).map({
			"PaymentMethodCode": "BANK", "Description": None
		})
		self.assertEqual(result["mode_of_payment"], "BANK")

	def test_new_catalog_mappings_validate_against_installed_doctypes(self):
		catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
		catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
		self.assertEqual({item["name"] for item in catalog if item["name"] in MAPPING_NAMES}, MAPPING_NAMES)
		for mapping in catalog:
			if mapping["name"] not in MAPPING_NAMES:
				continue
			with self.subTest(mapping=mapping["name"]):
				doc = frappe.get_doc({"doctype": "SAP Doctype Mapping", **mapping})
				doc.validate()

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

		original_get_all = frappe.get_all
		with patch("emkan_insights.sap.services.mapper.frappe.get_all") as get_all:
			get_all.side_effect = lambda doctype, **kwargs: (
				[] if doctype == "Item" else original_get_all(doctype, **kwargs)
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

	def test_fills_missing_child_uoms_from_matching_items(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Sales Order",
			field_mapping={
				"items": {
					"fieldname": "DocumentLines",
					"mapping": {
						"item_code": "ItemCode",
						"uom": "UoMCode",
					},
				}
			},
		)
		original_get_all = frappe.get_all
		with patch("emkan_insights.sap.services.mapper.frappe.get_all") as get_all:
			get_all.side_effect = lambda doctype, **kwargs: (
				[SimpleNamespace(item_code="ITEM-1", stock_uom="Nos")]
				if doctype == "Item" else original_get_all(doctype, **kwargs)
			)
			result = Mapper(mapping_doc).map({
				"DocumentLines": [
					{"ItemCode": "ITEM-1", "UoMCode": "Box"},
					{"ItemCode": "ITEM-1"},
					{"ItemCode": "MISSING"},
				]
			})

		self.assertEqual(result["items"], [
			{"item_code": "ITEM-1", "uom": "Box", "stock_uom": "Nos"},
			{"item_code": "ITEM-1", "uom": "Nos", "stock_uom": "Nos"},
			{"item_code": "MISSING", "uom": None},
		])
		self.assertEqual(sum(call.args[0] == "Item" for call in get_all.call_args_list), 1)

	def test_preserves_configured_service_layer_version(self):
		self.assertEqual(
			SAPB1Client._normalize_base_url("https://sap.example/b1s/v2/"),
			"https://sap.example/b1s/v2",
		)
		self.assertEqual(
			SAPB1Client._normalize_base_url("https://sap.example/b1s/v1/"),
			"https://sap.example/b1s/v1",
		)
		self.assertEqual(
			SAPB1Client._normalize_base_url("https://sap.example"),
			"https://sap.example/b1s/v2",
		)

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

	def test_maps_sap_boolean_values_from_json(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Item",
			field_mapping='{"disabled":{"fieldname":"Frozen","transform":"value_map",'
			'"transform_config":{"mapping":{"tYES":true,"tNO":false},"strict":true}}}',
		)
		mapper = Mapper(mapping_doc)

		self.assertIs(mapper.map({"Frozen": "tYES"})["disabled"], True)
		self.assertIs(mapper.map({"Frozen": "tNO"})["disabled"], False)
		with self.assertRaises(frappe.ValidationError):
			mapper.map({"Frozen": "unknown"})

	def test_value_map_normalizes_sap_boolean_case(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Item",
			field_mapping={
				"disabled": {
					"fieldname": "Frozen",
					"transform": "value_map",
					"transform_config": {
						"mapping": {"tyes": True},
						"default": False,
						"normalize": "lower",
					},
				}
			},
		)

		mapper = Mapper(mapping_doc)
		self.assertIs(mapper.map({"Frozen": "tYES"})["disabled"], True)
		self.assertIs(mapper.map({"Frozen": " tYes "})["disabled"], True)
		self.assertIs(mapper.map({"Frozen": "tNO"})["disabled"], False)

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

		with self.assertRaisesRegex(
			frappe.ValidationError,
			"due_date_based_on.*NumberOfAdditionalMonths=2.*StartFrom='pdt_None'",
		):
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

	def test_maps_parent_date_into_transfer_request_items(self):
		mapping_doc = SimpleNamespace(
			erp_doctype="Material Request",
			field_mapping={
				"items": {
					"fieldname": "StockTransferLines",
					"mapping": {
						"item_code": "ItemCode",
						"schedule_date": {"fieldname": "DueDate", "transform": "parent_date"},
					},
				}
			},
		)
		result = Mapper(mapping_doc).map({
			"DueDate": "2026-10-20T00:00:00Z",
			"StockTransferLines": [{"ItemCode": "ITEM-1"}],
		})
		self.assertEqual(result["items"][0]["schedule_date"], "2026-10-20")

	def test_combines_static_and_date_filters(self):
		mapping_doc = SimpleNamespace(
			sap_filter="CardType eq 'cCustomer'",
			date_filter_field="CreateDate",
		)

		params = _query_params(mapping_doc, "2026-01-01", "2026-01-31")

		self.assertEqual(
			params["$filter"],
			"(CardType eq 'cCustomer') and (CreateDate ge '2026-01-01' and CreateDate lt '2026-02-01')",
		)

	def test_filters_on_creation_or_update_date(self):
		mapping_doc = SimpleNamespace(
			sap_filter=None,
			creation_date_filter_field="CreateDate",
			date_filter_field="UpdateDate",
			full_snapshot_sync=0,
		)

		params = _query_params(mapping_doc, "2026-01-01", "2026-01-31")

		self.assertEqual(
			params["$filter"],
			"((CreateDate ge '2026-01-01' and CreateDate lt '2026-02-01') or "
			"(UpdateDate ge '2026-01-01' and UpdateDate lt '2026-02-01'))",
		)

	def test_full_snapshot_ignores_date_filter(self):
		mapping_doc = SimpleNamespace(
			sap_filter="CardType eq 'cCustomer'",
			creation_date_filter_field="CreateDate",
			date_filter_field="UpdateDate",
			full_snapshot_sync=1,
		)

		self.assertEqual(
			_query_params(mapping_doc, "2026-01-01", "2026-01-31"),
			{"$filter": "(CardType eq 'cCustomer')"},
		)

	def test_incremental_fetch_rejects_missing_date_fields(self):
		mapping_doc = SimpleNamespace(
			sap_filter=None,
			creation_date_filter_field=None,
			date_filter_field=None,
			full_snapshot_sync=0,
		)

		with self.assertRaises(frappe.ValidationError):
			_query_params(mapping_doc, None, None)

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
	def test_fetch_all_stops_at_review_limit(self):
		client = SAPB1Client("https://sap.example.com", "COMPANY", "user", "secret")
		client.get = Mock(return_value={
			"value": [{"JdtNum": 1}, {"JdtNum": 2}, {"JdtNum": 3}],
			"@odata.nextLink": "JournalEntries?$skip=3",
		})

		result = client.fetch_all("JournalEntries", max_records=2)

		self.assertEqual(result, [{"JdtNum": 1}, {"JdtNum": 2}])
		client.get.assert_called_once()

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
	def test_raw_ledger_source_stages_full_sap_record(self):
		mapping = SimpleNamespace(
			name="SAP GL Journal Detail",
			sap_doctype="JournalEntries",
			sap_id_field="JdtNum",
			sap_id_prefix="journal:",
			raw_payload=True,
		)
		writer = Mock()
		writer.upsert.return_value = ("created", "EXTERNAL-1")
		result = {"success": [], "created": [], "updated": [], "unchanged": [], "failed": []}
		record = {"JdtNum": 42, "JournalEntryLines": [{"Line_ID": 0, "Debit": 50}]}

		_process_record(mapping, None, writer, record, 0, result)

		writer.upsert.assert_called_once_with("journal:42", {
			"sap_mapping": mapping.name,
			"source_endpoint": "JournalEntries",
			"record": record,
		})
		self.assertEqual(result["created"], ["journal:42"])

	@patch("emkan_insights.sap.services.frappe")
	def test_record_error_stops_fetch(self, frappe_mock):
		mapping = SimpleNamespace(name="SAP Account", sap_id_field="Code", sap_id_prefix="")
		mapper = Mock()
		mapper.map.side_effect = frappe.ValidationError("No root_type match")
		writer = Mock()
		result = {"success": [], "created": [], "updated": [], "failed": []}

		with self.assertRaisesRegex(frappe.ValidationError, "No root_type match"):
			_process_record(mapping, mapper, writer, {"Code": "profit"}, 0, result)

		frappe_mock.db.rollback.assert_called_once_with(save_point="sap_sync_0")
		self.assertEqual(result["failed"], ["profit"])
		self.assertTrue(frappe_mock.log_error.call_args.kwargs["defer_insert"])
		writer.upsert.assert_not_called()

	@patch("emkan_insights.sap.services.frappe")
	def test_missing_sap_id_stops_fetch(self, frappe_mock):
		frappe_mock.ValidationError = frappe.ValidationError
		mapping = SimpleNamespace(name="SAP Account", sap_id_field="Code")

		with self.assertRaisesRegex(frappe.ValidationError, "SAP record #1.*has no Code"):
			_process_record(mapping, Mock(), Mock(), {"Name": "Missing code"}, 0, {})

	@patch("emkan_insights.sap.services.frappe")
	def test_stages_prefixed_sap_id(self, frappe_mock):
		mapping = SimpleNamespace(name="SAP Incoming Payment", sap_id_field="DocEntry", sap_id_prefix="incoming:")
		mapper = Mock()
		mapper.map.return_value = {"doctype": "Payment Entry"}
		writer = Mock()
		writer.upsert.return_value = ("created", "STAGE-1")
		result = {"success": [], "created": [], "updated": [], "failed": []}

		_process_record(mapping, mapper, writer, {"DocEntry": 7}, 0, result)

		writer.upsert.assert_called_once_with("incoming:7", {"doctype": "Payment Entry"})
		self.assertEqual(result["created"], ["incoming:7"])

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
		staging_doc.json = '{"doctype":"UOM","name":"Old"}'
		frappe_mock.db.get_value.return_value = "STAGE-1"
		frappe_mock.parse_json.return_value = {"doctype": "UOM", "name": "Old"}
		frappe_mock.get_doc.return_value = staging_doc

		action, name = StagingWriter("External Uom", "SAP Esacon").upsert(
			"KG", {"doctype": "UOM"}
		)

		self.assertEqual((action, name), ("updated", "STAGE-1"))
		self.assertNotIn("last_synced_on", staging_doc.update.call_args.args[0])
		staging_doc.save.assert_called_once_with(ignore_permissions=True)

	@patch("emkan_insights.sap.services.staging.frappe")
	def test_unchanged_staging_record_is_not_resynced(self, frappe_mock):
		staging_doc = Mock()
		staging_doc.name = "STAGE-1"
		staging_doc.json = '{"doctype":"UOM"}'
		frappe_mock.db.get_value.return_value = "STAGE-1"
		frappe_mock.parse_json.return_value = {"doctype": "UOM"}
		frappe_mock.get_doc.return_value = staging_doc

		action, name = StagingWriter("External Uom", "SAP Esacon").upsert(
			"KG", {"doctype": "UOM"}
		)

		self.assertEqual((action, name), ("unchanged", "STAGE-1"))
		staging_doc.save.assert_not_called()
