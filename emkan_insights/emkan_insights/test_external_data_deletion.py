from contextlib import nullcontext
from unittest.mock import Mock, call, patch

import frappe
from frappe import _dict
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_data_deletion import (
	DELETION_BATCH_JOB_ID,
	DELETION_LOCK_TTL,
	DeletionContext,
	_delete_rows_with_children,
	_fetch_all_remote_ids,
	_get_target_name,
	_reconcile_doctype_deletions,
	enqueue_external_data_deletion_batch,
	reconcile_external_data_deletion_batch,
)


MODULE = "emkan_insights.emkan_insights.external_data_deletion"


class TestExternalDataDeletion(FrappeTestCase):
	@patch(f"{MODULE}.frappe.enqueue", return_value=Mock())
	def test_batch_queues_one_job_for_all_configurations(self, enqueue):
		result = enqueue_external_data_deletion_batch(
			["CPC", "Incharge", "CPC"],
			enqueue_after_commit=False,
		)

		enqueue.assert_called_once_with(
			f"{MODULE}.reconcile_external_data_deletion_batch",
			queue="long",
			timeout=DELETION_LOCK_TTL,
			enqueue_after_commit=False,
			job_id=DELETION_BATCH_JOB_ID,
			deduplicate=True,
			configuration_names=["CPC", "Incharge"],
		)
		self.assertEqual(result["configurations"], 2)

	@patch("emkan_insights.emkan_insights.reconcile_missing_records.execute")
	@patch(f"{MODULE}._deletion_lock", return_value=nullcontext())
	def test_batch_reconciles_configurations_sequentially(
		self,
		deletion_lock,
		reconcile_deletions,
	):
		reconcile_deletions.side_effect = [
			{"status": "complete", "configuration": "CPC"},
			{"status": "complete", "configuration": "Incharge"},
		]

		result = reconcile_external_data_deletion_batch(["CPC", "Incharge"])

		self.assertEqual(
			reconcile_deletions.call_args_list,
			[
				call("CPC", doctype=None, dry_run=False, deletion_lock_acquired=True),
				call("Incharge", doctype=None, dry_run=False, deletion_lock_acquired=True),
			],
		)
		self.assertEqual(result["status"], "complete")
		deletion_lock.assert_called_once_with()

	def test_target_name_uses_company_abbreviation_for_configured_doctype(self):
		context = _context(abbr_doctypes=frozenset({"Customer"}))

		self.assertEqual(_get_target_name(context, "CUST-0001"), "CPC-CUST-0001")
		self.assertEqual(_get_target_name(context, "CPC-CUST-0001"), "CPC-CUST-0001")

	@patch(f"{MODULE}.requests.get")
	def test_complete_remote_snapshot_uses_name_cursor(self, get):
		first = Mock()
		first.json.return_value = {"data": [{"name": f"CUST-{index:04}"} for index in range(500)]}
		second = Mock()
		second.json.return_value = {"data": [{"name": "CUST-0500"}]}
		get.side_effect = [first, second]

		remote_ids = _fetch_all_remote_ids(_context())

		self.assertEqual(len(remote_ids), 501)
		second_filters = frappe.parse_json(get.call_args_list[1].kwargs["params"]["filters"])
		self.assertIn(["name", ">", "CUST-0499"], second_filters)

	@patch(f"{MODULE}.frappe.db.commit")
	@patch(f"{MODULE}._rebuild_tree_if_needed")
	@patch(f"{MODULE}._get_existing_target_names", return_value=["CPC-CUST-0002"])
	@patch(f"{MODULE}._delete_rows_with_children")
	@patch(f"{MODULE}._get_staging_rows")
	@patch(f"{MODULE}._fetch_all_remote_ids", return_value={"CUST-0001"})
	def test_reconciliation_bulk_deletes_prefixed_target_and_staging(
		self,
		_fetch_remote,
		get_staging,
		delete_rows,
		_get_existing,
		rebuild_tree_if_needed,
		_commit,
	):
		get_staging.return_value = {
			"CUST-0001": ["stage-1"],
			"CUST-0002": ["stage-2"],
		}
		context = _context(abbr_doctypes=frozenset({"Customer"}))

		result = _reconcile_doctype_deletions(context)

		self.assertEqual(
			delete_rows.call_args_list,
			[
				call("Customer", ["CPC-CUST-0002"]),
				call("External Customer", ["stage-2"]),
			],
		)
		self.assertEqual(result["deleted"], 1)
		self.assertEqual(result["failed"], 0)
		self.assertEqual(
			rebuild_tree_if_needed.call_args_list,
			[call("Customer"), call("External Customer")],
		)

	@patch(f"{MODULE}.frappe.clear_document_cache")
	@patch(f"{MODULE}.frappe.db.delete")
	@patch(f"{MODULE}.frappe.get_meta")
	def test_sql_deletion_removes_child_rows_before_parent(
		self,
		get_meta,
		delete,
		_clear_document_cache,
	):
		get_meta.return_value.get_table_fields.return_value = [
			_dict(options="Sales Invoice Item"),
			_dict(options="Sales Taxes and Charges"),
		]

		_delete_rows_with_children("Sales Invoice", ["CPC-SINV-0001"])

		self.assertEqual(
			delete.call_args_list,
			[
				call(
					"Sales Invoice Item",
					{
						"parent": ["in", ["CPC-SINV-0001"]],
						"parenttype": "Sales Invoice",
					},
				),
				call(
					"Sales Taxes and Charges",
					{
						"parent": ["in", ["CPC-SINV-0001"]],
						"parenttype": "Sales Invoice",
					},
				),
				call("Sales Invoice", {"name": ["in", ["CPC-SINV-0001"]]}),
			],
		)


def _context(**overrides):
	values = {
		"configuration": "CPC",
		"remote_doctype": "Customer",
		"staging_doctype": "External Customer",
		"target_doctype": "Customer",
		"site_url": "https://remote.example",
		"api_key": "key",
		"api_secret": "secret",
		"company_abbr": "CPC",
		"abbr_doctypes": frozenset(),
		"source_site_url": "https://remote.example",
	}
	values.update(overrides)
	return DeletionContext(**values)
