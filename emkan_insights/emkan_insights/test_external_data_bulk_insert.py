from unittest.mock import patch

import frappe
from frappe import _dict
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_data_bulk_insert import (
	BulkInsertContext,
	_bulk_insert_records,
	_insert_document_trees,
	_lock_key,
	_ordered_configuration_rows,
	_partition_document_trees,
	_pending_source_condition,
	_prepare_document_tree,
	_prepare_standard_fields,
	_source_site_condition,
	_summarize_results,
	_update_document_trees,
	add_prefix,
	transform_row,
)
from emkan_insights.emkan_insights.external_data_fetch import is_deferred_naming_pending
from emkan_insights.emkan_insights.import_field_rules import apply_field_rules, parse_field_rules


class TestExternalDataBulkInsert(FrappeTestCase):
	def test_import_field_rules_fill_missing_defaults_and_join_names(self):
		item_rules = parse_field_rules('{"weight_per_unit": {"default": 0}}', "Item")
		self.assertEqual(apply_field_rules({"weight_per_unit": None}, item_rules)["weight_per_unit"], 0)
		self.assertEqual(apply_field_rules({"weight_per_unit": 2}, item_rules)["weight_per_unit"], 2)

		name_rules = parse_field_rules(
			{"full_name": {"join": ["first_name", "last_name"], "separator": " "}},
			"Contact",
		)
		self.assertEqual(
			apply_field_rules({"first_name": "Ada", "last_name": "Lovelace"}, name_rules)["full_name"],
			"Ada Lovelace",
		)
		self.assertNotIn("full_name", apply_field_rules({"first_name": "Ada"}, name_rules))

	def test_import_field_rules_reject_unknown_fields(self):
		with self.assertRaises(frappe.ValidationError):
			parse_field_rules({"weight_per_unit": {"join": ["missing", "stock_uom"]}}, "Item")

	def test_prepare_document_tree_applies_configured_default(self):
		context = BulkInsertContext(
			configuration="Remote Site",
			source_doctype="External Item",
			target_doctype="Item",
			company_abbr="",
			abbr_doctypes=frozenset(),
			requested_by="Administrator",
			field_rules={"weight_per_unit": {"default": 0}},
		)
		result = _prepare_document_tree(context, {"name": "ITEM-1", "weight_per_unit": None})
		self.assertEqual(result["Item"][0]["weight_per_unit"], 0)

	def test_temporary_ledger_rows_are_not_ready_for_import(self):
		self.assertTrue(is_deferred_naming_pending("GL Entry", {"to_rename": 1}))
		self.assertTrue(is_deferred_naming_pending("Stock Ledger Entry", {"to_rename": "1"}))
		self.assertFalse(is_deferred_naming_pending("GL Entry", {"to_rename": 0}))
		self.assertFalse(is_deferred_naming_pending("Sales Invoice", {"to_rename": 1}))

	def test_bulk_insert_skips_staged_temporary_ledger_row(self):
		context = BulkInsertContext(
			configuration="Remote Site",
			source_doctype="External GL Entry",
			target_doctype="GL Entry",
			company_abbr="CMP",
			abbr_doctypes=frozenset({"GL Entry"}),
			requested_by="Administrator",
		)
		source_row = _dict(
			name="staging-row",
			remote_id="temporary-hash",
			json='{"name": "temporary-hash", "to_rename": 1}',
		)

		with (
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._validate_source"
			),
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._count_pending_source_rows",
				return_value=1,
			),
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._get_pending_source_rows",
				side_effect=[[source_row], []],
			),
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._partition_document_trees"
			) as partition,
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._mark_source_rows_synced"
			) as mark_synced,
			patch.object(frappe.db, "commit"),
			patch.object(frappe, "log_error"),
		):
			result = _bulk_insert_records(context, "2026-09-10 10:00:00")

		self.assertEqual(result["inserted"], 0)
		self.assertEqual(result["updated"], 0)
		partition.assert_not_called()
		mark_synced.assert_called_once_with("External GL Entry", [source_row])

	def test_configuration_rows_are_processed_in_child_table_order(self):
		configuration = _dict(fetch_data=[
			_dict(idx=3, ref_doctype="Sales Invoice"),
			_dict(idx=1, ref_doctype="Company"),
			_dict(idx=2, ref_doctype="Customer"),
		])

		rows = _ordered_configuration_rows(configuration)

		self.assertEqual([row.ref_doctype for row in rows], ["Company", "Customer", "Sales Invoice"])

	def test_results_are_summarized_across_all_doctypes(self):
		result = _summarize_results("Remote Site", "2026-09-08 10:00:00", [
			{"inserted": 2, "updated": 1},
			{"inserted": 3, "updated": 4},
		])

		self.assertEqual(result["configuration"], "Remote Site")
		self.assertEqual(result["started_at"], "2026-09-08 10:00:00")
		self.assertEqual(result["processed_doctypes"], 2)
		self.assertEqual(result["inserted"], 5)
		self.assertEqual(result["updated"], 5)

	def test_document_trees_are_partitioned_by_existing_parent(self):
		documents = [
			{"Quotation": [{"name": "QTN-0001"}]},
			{"Quotation": [{"name": "QTN-0002"}]},
		]

		with patch.object(frappe, "get_all", return_value=["QTN-0002"]):
			new_documents, existing_documents = _partition_document_trees("Quotation", documents)

		self.assertEqual(new_documents, [documents[0]])
		self.assertEqual(existing_documents, [documents[1]])

	def test_existing_document_tree_updates_parent_and_replaces_children(self):
		documents = [{
			"Quotation": [{"name": "QTN-0001", "title": "Updated", "unknown": "ignored"}],
			"Quotation Item": [{"name": "ITEM-1", "parent": "QTN-0001", "qty": 2}],
		}]

		with (
			patch.object(frappe.db, "get_table_columns", side_effect=[
				["name", "title"],
				["name", "parent", "qty"],
			]),
			patch.object(frappe.db, "bulk_update") as bulk_update,
			patch.object(frappe.db, "delete") as delete,
			patch.object(frappe.db, "bulk_insert") as bulk_insert,
			patch.object(frappe, "get_meta") as get_meta,
		):
			get_meta.return_value.get_table_fields.return_value = [
				_dict(options="Quotation Item")
			]
			updated = _update_document_trees("Quotation", documents)

		self.assertEqual(updated, 1)
		bulk_update.assert_called_once_with(
			"Quotation",
			{"QTN-0001": {"title": "Updated"}},
			chunk_size=500,
			update_modified=False,
		)
		delete.assert_called_once_with(
			"Quotation Item",
			{"parent": ["in", ["QTN-0001"]], "parenttype": "Quotation"},
		)
		bulk_insert.assert_called_once_with(
			"Quotation Item",
			fields=["name", "parent", "qty"],
			values=[("ITEM-1", "QTN-0001", 2)],
			ignore_duplicates=True,
			chunk_size=500,
		)

	def test_pending_source_condition_selects_new_or_modified_rows_from_job_snapshot(self):
		context = BulkInsertContext(
			configuration="Remote Site",
			source_doctype="External Customer",
			target_doctype="Customer",
			company_abbr="CMP",
			abbr_doctypes=frozenset(),
			requested_by="Administrator",
		)
		started_at = "2026-09-08 10:00:00"
		source = frappe.qb.DocType(context.source_doctype)

		with patch.object(
			frappe.db,
			"get_table_columns",
			return_value=["source_site", "json", "last_synced_on", "modified"],
		):
			query = (
				frappe.qb.from_(source)
				.select(source.name)
				.where(_pending_source_condition(source, context, started_at))
			)
		sql = query.get_sql()

		self.assertIn("`last_synced_on` IS NULL", sql)
		self.assertIn("`modified`>`last_synced_on`", sql)
		self.assertIn("`modified`<='2026-09-08 10:00:00'", sql)

	def test_source_condition_matches_legacy_site_url_rows(self):
		context = BulkInsertContext(
			configuration="CPC",
			source_doctype="External Purchase Order",
			target_doctype="Purchase Order",
			company_abbr="CPC",
			abbr_doctypes=frozenset(),
			requested_by="Administrator",
			source_site_url="https://cpc.example.com/",
		)
		source = frappe.qb.DocType(context.source_doctype)

		with patch.object(
			frappe.db,
			"get_table_columns",
			return_value=["source_site", "site_url"],
		):
			condition = _source_site_condition(source, context)

		query = frappe.qb.from_(source).select(source.name).where(condition)
		sql = query.get_sql()
		self.assertIn("`source_site`='CPC'", sql)
		self.assertIn("`site_url` IN ('https://cpc.example.com','https://cpc.example.com/')", sql)

	def test_bulk_insert_uses_the_configuration_pipeline_lock(self):
		self.assertEqual(
			_lock_key("Remote Site"),
			"external_site_pipeline_lock:Remote Site",
		)

	def test_bulk_insert_uses_only_physical_table_columns(self):
		documents = [{
			"Quotation": [{
				"name": "QTN-0001",
				"title": "Quotation",
				"last_scanned_warehouse": "Stores - CMP",
			}],
		}]

		with (
			patch.object(frappe.db, "get_table_columns", return_value=["name", "title"]),
			patch.object(frappe.db, "bulk_insert") as bulk_insert,
		):
			inserted = _insert_document_trees(documents)

		self.assertEqual(inserted, 1)
		bulk_insert.assert_called_once_with(
			"Quotation",
			fields=["name", "title"],
			values=[("QTN-0001", "Quotation")],
			ignore_duplicates=True,
			chunk_size=500,
		)

	def test_bulk_insert_uses_database_default_for_null_non_nullable_field(self):
		documents = [{"Item": [{"name": "ITEM-1", "weight_per_unit": None}]}]
		with (
			patch.object(frappe.db, "get_table_columns", return_value=["name", "weight_per_unit"]),
			patch.object(
				frappe.db, "sql",
				return_value=[_dict(COLUMN_NAME="weight_per_unit", COLUMN_DEFAULT="0.000000000")],
			),
			patch.object(frappe.db, "bulk_insert") as bulk_insert,
		):
			_insert_document_trees(documents)

		self.assertEqual(bulk_insert.call_args.kwargs["values"], [("ITEM-1", "0.000000000")])

	def test_account_bulk_insert_excludes_nested_set_fields(self):
		documents = [{
			"Account": [{
				"name": "Assets - CMP",
				"parent_account": "",
				"lft": 10,
				"rgt": 20,
				"old_parent": "Old Assets - CMP",
			}],
		}]

		with (
			patch.object(
				frappe.db,
				"get_table_columns",
				return_value=["name", "parent_account", "lft", "rgt", "old_parent"],
			),
			patch.object(frappe.db, "bulk_insert") as bulk_insert,
		):
			inserted = _insert_document_trees(documents)

		self.assertEqual(inserted, 1)
		bulk_insert.assert_called_once_with(
			"Account",
			fields=["name", "parent_account"],
			values=[("Assets - CMP", "")],
			ignore_duplicates=True,
			chunk_size=500,
		)

	def test_account_bulk_update_excludes_nested_set_fields(self):
		documents = [{
			"Account": [{
				"name": "Assets - CMP",
				"parent_account": "",
				"lft": 10,
				"rgt": 20,
				"old_parent": "Old Assets - CMP",
			}],
		}]

		with (
			patch.object(
				frappe.db,
				"get_table_columns",
				return_value=["name", "parent_account", "lft", "rgt", "old_parent"],
			),
			patch.object(frappe.db, "bulk_update") as bulk_update,
			patch.object(frappe, "get_meta") as get_meta,
		):
			get_meta.return_value.get_table_fields.return_value = []
			updated = _update_document_trees("Account", documents)

		self.assertEqual(updated, 1)
		bulk_update.assert_called_once_with(
			"Account",
			{"Assets - CMP": {"parent_account": ""}},
			chunk_size=500,
			update_modified=False,
		)

	def test_account_tree_is_rebuilt_after_bulk_import(self):
		context = BulkInsertContext(
			configuration="Remote Site",
			source_doctype="External Account",
			target_doctype="Account",
			company_abbr="CMP",
			abbr_doctypes=frozenset(),
			requested_by="Administrator",
		)

		with (
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._validate_source"
			),
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._count_pending_source_rows",
				return_value=0,
			),
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert._get_pending_source_rows",
				return_value=[],
			),
			patch(
				"emkan_insights.emkan_insights.external_data_bulk_insert.rebuild_tree"
			) as rebuild_tree,
			patch.object(frappe, "log_error"),
		):
			result = _bulk_insert_records(context, "2026-09-08 10:00:00")

		self.assertEqual(result["status"], "complete")
		rebuild_tree.assert_called_once_with("Account", "parent_account")

	def test_add_prefix_is_idempotent(self):
		self.assertEqual(add_prefix("SO-0001", "CMP"), "CMP-SO-0001")
		self.assertEqual(add_prefix("CMP-SO-0001", "CMP"), "CMP-SO-0001")

	def test_transform_row_prefixes_configured_links(self):
		row = {
			"name": "SLE-0001",
			"item_code": "ITEM-1",
			"warehouse": "Stores - CMP",
			"voucher_type": "Stock Entry",
			"voucher_no": "STE-0001",
		}
		link_fields = {
			"item_code": "Item",
			"warehouse": "Warehouse",
			"voucher_no": {"options_field": "voucher_type"},
		}

		result = transform_row(
			"Stock Ledger Entry",
			row,
			"CMP",
			{"Stock Ledger Entry", "Item", "Stock Entry"},
			link_fields=link_fields,
		)

		self.assertEqual(result["name"], "CMP-SLE-0001")
		self.assertEqual(result["item_code"], "CMP-ITEM-1")
		self.assertEqual(result["warehouse"], "Stores - CMP")
		self.assertEqual(result["voucher_no"], "CMP-STE-0001")

	def test_transform_row_keeps_name_when_abbr_is_not_required(self):
		result = transform_row(
			"GL Entry",
			{"name": "GLE-0001", "account": "Debtors - CMP"},
			"CMP",
			set(),
			link_fields={"account": "Account"},
		)

		self.assertEqual(result["name"], "GLE-0001")
		self.assertEqual(result["account"], "Debtors - CMP")

	def test_standard_field_preparation_preserves_source_timestamps(self):
		row = {
			"creation": "2025-01-02 03:04:05.000001",
			"modified": "2025-06-07 08:09:10.000002",
		}

		result = _prepare_standard_fields(row, "Administrator", "2026-01-01 00:00:00")

		self.assertEqual(result["creation"], row["creation"])
		self.assertEqual(result["modified"], row["modified"])
