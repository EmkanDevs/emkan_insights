import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_data_bulk_insert import BulkInsertContext
from emkan_insights.sap.services.transforms import apply_transform
from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.sync import (
	_log,
	_get_target_names_by_remote_id,
	_prepare_source_row,
	_skip_conflicting_unique_values,
	_skip_duplicate_item_groups,
	sync_mapping,
)


class TestSAPSync(FrappeTestCase):
	def test_held_journal_raw_response_cannot_sync(self):
		context = SimpleNamespace(target_doctype="Journal Entry")
		source = SimpleNamespace(
			name="STAGE-1", remote_id="42",
			json={"sap_mapping": "SAP Journal Entry", "record": {"JdtNum": 42}},
		)
		with self.assertRaisesRegex(frappe.ValidationError, "held SAP record"):
			_prepare_source_row(context, source)

	def test_submitted_accounting_and_stock_rows_need_erp_submission(self):
		for doctype in ("Journal Entry", "Purchase Invoice", "Stock Entry", "Purchase Receipt"):
			with self.subTest(doctype=doctype):
				context = SimpleNamespace(target_doctype=doctype)
				source = SimpleNamespace(name="STAGE-1", remote_id="42", json={"docstatus": 1})
				with self.assertRaisesRegex(frappe.ValidationError, "ERPNext submission"):
					_prepare_source_row(context, source)

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: {"Purchase Invoice": [raw]})
	def test_draft_accounting_row_can_remain_staged_for_validation(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname=None, target_doctype="Purchase Invoice")
		source = SimpleNamespace(name="STAGE-1", remote_id="42", json={"docstatus": 0})
		result = _prepare_source_row(context, source)
		self.assertEqual(result["Purchase Invoice"][0]["docstatus"], 0)

	@patch("emkan_insights.sap.sync._prepare_document_tree", return_value={"Purchase Invoice": [{"docstatus": 1}]})
	def test_site_rule_cannot_submit_accounting_row_through_bulk_writer(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname=None, target_doctype="Purchase Invoice")
		source = SimpleNamespace(name="STAGE-1", remote_id="42", json={"docstatus": 0})
		with self.assertRaisesRegex(frappe.ValidationError, "ERPNext submission"):
			_prepare_source_row(context, source)

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_new_account_id_matches_mapped_account_name(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname=None, target_doctype="Account")
		source = SimpleNamespace(
			name="STAGE-1",
			remote_id="1000",
			json={"account_name": "Cash - 1000", "account_number": "1000"},
		)

		result = _prepare_source_row(context, source)

		self.assertEqual(result["name"], result["account_name"])
		self.assertEqual(result["name"], "Cash - 1000")

	@patch("emkan_insights.sap.sync.frappe.get_all")
	def test_existing_account_with_legacy_id_is_reused(self, get_all):
		get_all.return_value = ["1000"]
		context = SimpleNamespace(
			target_doctype="Account", erp_sap_id_fieldname=None,
			bulk=SimpleNamespace(company_abbr="", abbr_doctypes=frozenset()),
		)
		source = SimpleNamespace(remote_id="1000")

		self.assertEqual(_get_target_names_by_remote_id(context, [source]), {"1000": "1000"})
		get_all.assert_called_once_with(
			"Account", filters={"name": ["in", ["1000"]]},
			pluck="name", limit_page_length=0,
		)

	@patch("emkan_insights.sap.sync._log")
	@patch("emkan_insights.sap.sync.frappe.get_all")
	@patch("emkan_insights.sap.sync.frappe.get_meta")
	def test_skips_only_manufacturer_with_conflicting_short_name(self, get_meta, get_all, log):
		get_meta.return_value.fields = [SimpleNamespace(fieldname="short_name", unique=1)]
		get_all.return_value = [frappe._dict(name="Audi", short_name="Audi")]
		context = SimpleNamespace(target_doctype="Manufacturer")
		rows = [
			SimpleNamespace(name="STAGE-1", remote_id="-1"),
			SimpleNamespace(name="STAGE-2", remote_id="2"),
		]
		documents = [
			{"Manufacturer": [{"name": "-1", "short_name": "Audi"}]},
			{"Manufacturer": [{"name": "2", "short_name": "TOYOTA"}]},
		]

		accepted_rows, accepted, skipped = _skip_conflicting_unique_values(context, rows, documents)

		self.assertEqual(accepted_rows, [rows[1]])
		self.assertEqual(accepted, [documents[1]])
		self.assertEqual(skipped, 1)
		self.assertEqual(log.call_args.kwargs["existing_document"], "Audi")
		self.assertEqual(log.call_args.kwargs["remote_id"], "-1")

	@patch("emkan_insights.sap.sync._log")
	@patch("emkan_insights.sap.sync.frappe.get_all", return_value=[])
	@patch("emkan_insights.sap.sync.frappe.get_meta")
	def test_skips_second_unique_value_in_same_batch(self, get_meta, _get_all, log):
		get_meta.return_value.fields = [SimpleNamespace(fieldname="short_name", unique=1)]
		context = SimpleNamespace(target_doctype="Manufacturer")
		rows = [
			SimpleNamespace(name="STAGE-1", remote_id="1"),
			SimpleNamespace(name="STAGE-2", remote_id="2"),
		]
		documents = [
			{"Manufacturer": [{"name": "1", "short_name": "Audi"}]},
			{"Manufacturer": [{"name": "2", "short_name": "audi"}]},
		]

		accepted_rows, accepted, skipped = _skip_conflicting_unique_values(context, rows, documents)

		self.assertEqual(accepted_rows, [rows[0]])
		self.assertEqual(accepted, [documents[0]])
		self.assertEqual(skipped, 1)
		self.assertEqual(log.call_args.kwargs["existing_document"], "1")

	def test_item_name_keeps_full_sap_value(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		mapping = next(row for row in catalog if row["name"] == "SAP Item")
		fields = mapping["field_mapping"]
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Item",
			field_mapping={field: fields[field] for field in ("item_name", "custom_sap_full_item_name", "description")},
		))
		full_name = "N" * 200

		result = mapper.map({"ItemName": full_name, "User_Text": "SAP description"})

		self.assertEqual(result["item_name"], full_name[:140])
		self.assertEqual(result["custom_sap_full_item_name"], full_name)
		self.assertEqual(result["description"], "SAP description")

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_existing_staged_item_name_is_shortened_without_losing_full_name(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname=None, target_doctype="Item")
		full_name = "N" * 200
		source = SimpleNamespace(
			name="STAGE-1", remote_id="ITEM-1", json={"item_name": full_name, "description": "SAP description"}
		)

		result = _prepare_source_row(context, source)

		self.assertEqual(result["item_name"], full_name[:140])
		self.assertEqual(result["custom_sap_full_item_name"], full_name)
		self.assertEqual(result["description"], "SAP description")

	def test_item_weight_defaults_to_zero_when_sap_omits_it(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		mapping = next(row for row in catalog if row["name"] == "SAP Item")
		rule = mapping["field_mapping"]["weight_per_unit"]
		self.assertEqual(rule, {"fieldname": "InventoryWeight", "default": 0})
		mapper = Mapper(SimpleNamespace(erp_doctype="Item", field_mapping={"weight_per_unit": rule}))
		self.assertEqual(mapper.map({"InventoryWeight": None})["weight_per_unit"], 0)
		self.assertEqual(mapper.map({"InventoryWeight": 1.5})["weight_per_unit"], 1.5)

	def test_staged_item_weight_uses_default_during_sync(self):
		bulk = BulkInsertContext(
			configuration="SAP Test",
			source_doctype="External Item",
			target_doctype="Item",
			company_abbr="",
			abbr_doctypes=frozenset(),
			requested_by="Administrator",
			field_rules={"weight_per_unit": {"default": 0}},
		)
		context = SimpleNamespace(bulk=bulk, erp_sap_id_fieldname=None, target_doctype="Item")
		source = SimpleNamespace(name="STAGE-1", remote_id="ITEM-1", json={"weight_per_unit": None})
		result = _prepare_source_row(context, source)
		self.assertEqual(result["Item"][0]["weight_per_unit"], 0)

	@patch("emkan_insights.sap.sync.frappe.get_traceback", return_value="insert failed")
	@patch("emkan_insights.sap.sync.frappe.db.rollback")
	@patch("emkan_insights.sap.sync._log")
	@patch("emkan_insights.sap.sync._insert_document_trees", side_effect=ValueError("bad row"))
	@patch("emkan_insights.sap.sync._update_document_trees", return_value=0)
	@patch("emkan_insights.sap.sync._partition_document_trees", return_value=([{}], []))
	@patch("emkan_insights.sap.sync._prepare_source_row", return_value={"Item": [{"name": "ITEM-1"}]})
	@patch("emkan_insights.sap.sync._get_target_names_by_remote_id", return_value={})
	@patch("emkan_insights.sap.sync._get_pending_rows")
	@patch("emkan_insights.sap.sync._count_pending_rows", return_value=1)
	@patch("emkan_insights.sap.sync._validate_source")
	@patch("emkan_insights.sap.sync.get_sync_context")
	def test_failed_batch_log_identifies_source_ids(
		self, get_context, _validate, _count, pending_rows, _names, _prepare, _partition,
		_update, _insert, log, rollback, _traceback,
	):
		get_context.return_value = SimpleNamespace(
			source_doctype="External Item", target_doctype="Item", mapping_name="SAP Item"
		)
		pending_rows.return_value = [SimpleNamespace(name="STAGE-1", remote_id="ITEM-1")]
		with self.assertRaisesRegex(ValueError, "bad row"):
			sync_mapping("ROW")
		rollback.assert_called_once()
		self.assertEqual(log.call_args.args[1], "Failed")
		self.assertEqual(log.call_args.kwargs["remote_ids"], ["ITEM-1"])

	def test_item_lead_time_defaults_to_zero_when_sap_omits_it(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		mapping = next(row for row in catalog if row["name"] == "SAP Item")
		rule = mapping["field_mapping"]["lead_time_days"]

		self.assertEqual(apply_transform(None, rule), 0)
		self.assertEqual(apply_transform(5, rule), 5)

	@patch("emkan_insights.sap.sync.frappe.get_meta")
	@patch("emkan_insights.sap.sync.frappe.db.commit")
	@patch("emkan_insights.sap.sync._mark_rows_synced")
	@patch("emkan_insights.sap.sync._prepare_source_row")
	@patch("emkan_insights.sap.sync._get_target_names_by_remote_id", return_value={})
	@patch("emkan_insights.sap.sync._get_pending_rows")
	@patch("emkan_insights.sap.sync._count_pending_rows", return_value=1)
	@patch("emkan_insights.sap.sync._validate_source")
	@patch("emkan_insights.sap.sync.get_sync_context")
	def test_updates_existing_documents_before_inserting_new_ones(
		self, get_context, _validate, _count, pending_rows, _target_names,
		prepare_row, _mark_synced, _commit, get_meta,
	):
		get_context.return_value = SimpleNamespace(
			source_doctype="External Item Group", target_doctype="Item Group", mapping_name="SAP Item Group"
		)
		pending_rows.side_effect = [[SimpleNamespace(remote_id="104")], []]
		prepare_row.return_value = {"Item Group": [{"name": "104"}]}
		get_meta.return_value.is_tree = False
		events = []
		with (
			patch("emkan_insights.sap.sync._partition_document_trees", return_value=([{}], [{}])),
			patch("emkan_insights.sap.sync._update_document_trees", side_effect=lambda *_: events.append("update") or 1),
			patch("emkan_insights.sap.sync._insert_document_trees", side_effect=lambda *_: events.append("insert") or 1),
			patch("emkan_insights.sap.sync._log") as log,
		):
			sync_mapping("ROW")

		self.assertEqual(events, ["update", "insert"])
		self.assertEqual(log.call_args.args[1], "DocType Creation Completed")
		self.assertEqual(log.call_args.kwargs, {"inserted": 1, "updated": 1, "skipped": 0})

	@patch("emkan_insights.sap.sync.frappe.get_meta")
	@patch("emkan_insights.sap.sync.frappe.db.commit")
	@patch("emkan_insights.sap.sync._mark_rows_synced")
	@patch("emkan_insights.sap.sync._insert_document_trees")
	@patch("emkan_insights.sap.sync._update_document_trees")
	@patch("emkan_insights.sap.sync._prepare_source_row")
	@patch("emkan_insights.sap.sync._get_target_names_by_remote_id", return_value={})
	@patch("emkan_insights.sap.sync._get_pending_rows")
	@patch("emkan_insights.sap.sync._count_pending_rows", return_value=1)
	@patch("emkan_insights.sap.sync._validate_source")
	@patch("emkan_insights.sap.sync.get_sync_context")
	def test_duplicate_item_group_is_marked_processed_without_writing_document(
		self, get_context, _validate, _count, pending_rows, _target_names,
		prepare_row, update, insert, mark_synced, _commit, get_meta,
	):
		get_context.return_value = SimpleNamespace(
			source_doctype="External Item Group", target_doctype="Item Group", mapping_name="SAP Item Group"
		)
		source = SimpleNamespace(name="STAGE-1", remote_id="104")
		pending_rows.side_effect = [[source], []]
		prepare_row.return_value = {
			"Item Group": [{"name": "SAP-104", "item_group_name": "CONSUMABLES"}]
		}
		get_meta.return_value.is_tree = False
		with (
			patch("emkan_insights.sap.sync.frappe.get_all", return_value=[
				SimpleNamespace(name="EXISTING", item_group_name="CONSUMABLES")
			]),
			patch("emkan_insights.sap.sync._log"),
		):
			result = sync_mapping("ROW")

		self.assertEqual(result["skipped"], 1)
		update.assert_not_called()
		insert.assert_not_called()
		mark_synced.assert_called_once_with("External Item Group", [source])

	@patch("emkan_insights.sap.sync._log")
	@patch("emkan_insights.sap.sync.frappe.get_all")
	def test_skips_item_group_name_owned_by_another_document(self, get_all, log):
		context = SimpleNamespace(target_doctype="Item Group")
		source_rows = [SimpleNamespace(name="STAGE-1"), SimpleNamespace(name="STAGE-2")]
		documents = [
			{"Item Group": [{"name": "105", "item_group_name": "SERVICES"}]},
			{"Item Group": [{"name": "SAP-2", "item_group_name": "SUPPLIES"}]},
		]
		get_all.return_value = [SimpleNamespace(name="102", item_group_name="Services")]

		accepted, skipped = _skip_duplicate_item_groups(context, source_rows, documents)

		self.assertEqual(accepted, [documents[1]])
		self.assertEqual(skipped, 1)
		self.assertEqual(log.call_args.args[1], "Skipped Duplicate Item Group")
		self.assertEqual(log.call_args.kwargs["existing_item_group"], "102")
		get_all.assert_called_once()

	@patch("emkan_insights.sap.sync._log")
	@patch("emkan_insights.sap.sync.frappe.get_all", return_value=[])
	def test_skips_second_item_group_with_same_name_in_batch(self, _get_all, log):
		context = SimpleNamespace(target_doctype="Item Group")
		source_rows = [SimpleNamespace(name="STAGE-1"), SimpleNamespace(name="STAGE-2")]
		documents = [
			{"Item Group": [{"name": "SAP-1", "item_group_name": "CONSUMABLES"}]},
			{"Item Group": [{"name": "SAP-2", "item_group_name": "Consumables"}]},
		]

		accepted, skipped = _skip_duplicate_item_groups(context, source_rows, documents)

		self.assertEqual(accepted, [documents[0]])
		self.assertEqual(skipped, 1)
		self.assertEqual(log.call_args.kwargs["source_row"], "STAGE-2")

	@patch("emkan_insights.sap.sync._log")
	@patch("emkan_insights.sap.sync.frappe.get_all")
	def test_allows_item_group_to_update_its_own_name(self, get_all, log):
		context = SimpleNamespace(target_doctype="Item Group")
		documents = [{"Item Group": [{"name": "EXISTING", "item_group_name": "CONSUMABLES"}]}]
		get_all.return_value = [SimpleNamespace(name="EXISTING", item_group_name="CONSUMABLES")]

		accepted, skipped = _skip_duplicate_item_groups(
			context, [SimpleNamespace(name="STAGE-1")], documents
		)

		self.assertEqual(accepted, documents)
		self.assertEqual(skipped, 0)
		log.assert_not_called()

	@patch("emkan_insights.sap.sync.frappe.log_error")
	def test_creation_completion_writes_configuration_error_log(self, log_error):
		context = SimpleNamespace(
			configuration="SAP - Company",
			mapping_name="SAP Item",
			source_doctype="External Item",
			target_doctype="Item",
		)

		_log(context, "DocType Creation Completed", inserted=3, updated=2)

		log_error.assert_called_once()
		entry = log_error.call_args.kwargs
		self.assertEqual(entry["title"], "SAP DocType Creation Completed: Item")
		self.assertIn("Inserted: 3", entry["message"])
		self.assertIn("Updated: 2", entry["message"])
		self.assertEqual(entry["reference_doctype"], "External SAP Configuration")
		self.assertEqual(entry["reference_name"], "SAP - Company")

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_prepares_stable_remote_name_and_sap_id(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname="custom_sap_id", target_doctype="Item")
		source = SimpleNamespace(
			name="STAGE-1",
			remote_id=42,
			json='{"item_name":"Example"}',
		)

		result = _prepare_source_row(context, source)

		self.assertEqual(result["name"], "42")
		self.assertEqual(result["custom_sap_id"], "42")

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_existing_target_name_is_preserved(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname="custom_sap_id", target_doctype="Item")
		source = SimpleNamespace(name="STAGE-1", remote_id="42", json={"item_name": "Example"})

		result = _prepare_source_row(context, source, target_name="EXISTING-TARGET")

		self.assertEqual(result["name"], "EXISTING-TARGET")

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_fills_item_uom_in_existing_staged_payload(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname=None, target_doctype="Sales Order")
		source = SimpleNamespace(
			name="STAGE-1",
			remote_id="42",
			json={"items": [{"item_code": "ITEM-1", "qty": 2}]},
		)
		original_get_all = frappe.get_all
		with patch("emkan_insights.sap.services.mapper.frappe.get_all") as get_all:
			get_all.side_effect = lambda doctype, **kwargs: (
				[SimpleNamespace(item_code="ITEM-1", stock_uom="Nos")]
				if doctype == "Item" else original_get_all(doctype, **kwargs)
			)
			result = _prepare_source_row(context, source)

		self.assertEqual(result["items"], [
			{"item_code": "ITEM-1", "qty": 2, "uom": "Nos", "stock_uom": "Nos"}
		])
