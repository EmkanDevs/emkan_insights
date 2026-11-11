from types import SimpleNamespace
from unittest.mock import call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration import (
	fetch_and_sync_configuration,
	fetch_configuration,
	fetch_mapping,
	queue_sync_mapping,
)
from emkan_insights.sap.tasks import (
	_ensure_company_cost_centers,
	_job_id,
	_configuration_synchronization_job_id,
	enqueue_sap_synchronization,
	fetch_sap_review_configuration,
	fetch_sap_data,
	synchronize_sap_configurations,
	synchronize_sap_configuration,
	synchronize_sap_data,
	synchronize_sap_doctype,
	sync_sap_data,
)


class TestSAPTasks(FrappeTestCase):
	@patch("emkan_insights.patches.v0_0_1.create_sap_company_account_roots.execute")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.execute")
	@patch("emkan_insights.patches.v0_0_1.create_sap_default_cost_centers.execute")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_cost_center_setup_does_not_create_accounts(
		self, frappe_mock, default_cost_centers, root_cost_centers, account_roots
	):
		_ensure_company_cost_centers()

		account_roots.assert_not_called()
		default_cost_centers.assert_called_once_with()
		root_cost_centers.assert_called_once_with()
		frappe_mock.db.commit.assert_called_once_with()

	@patch("emkan_insights.sap.tasks._ordered_mapping_rows", side_effect=lambda rows: rows)
	@patch("emkan_insights.sap.tasks.synchronize_sap_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_doctype_sync_runs_only_selected_mapping_rows(self, frappe_mock, sync_row, _ordered_rows):
		frappe_mock.get_all.side_effect = [
			["SAP Stock Entry Receipt", "SAP Stock Entry Issue"],
			[
				SimpleNamespace(name="ROW-1", parent="Emkan", sap_mapping="SAP Stock Entry Receipt"),
				SimpleNamespace(name="ROW-2", parent="Emkan", sap_mapping="SAP Stock Entry Issue"),
			],
		]
		sync_row.return_value = {"success": ["65"], "updated": ["65"], "failed": []}

		result = synchronize_sap_doctype(
			"Stock Entry", "Emkan", fetch_only=True,
			from_date="2025-01-01", to_date="2026-10-31",
		)

		self.assertEqual(result["configuration"], "Emkan")
		self.assertEqual([row["mapping"] for row in result["results"]], [
			"SAP Stock Entry Receipt", "SAP Stock Entry Issue",
		])
		self.assertEqual(result["results"][0]["fetched"], 1)
		self.assertEqual(sync_row.call_args_list, [
			call("ROW-1", fetch_only=True, from_date="2025-01-01", to_date="2026-10-31"),
			call("ROW-2", fetch_only=True, from_date="2025-01-01", to_date="2026-10-31"),
		])

	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={"success": ["issue:37"], "failed": []})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_explicit_date_window_is_passed_to_fetch(self, frappe_mock, get_mapped_data):
		row = self._row()
		mapping = SimpleNamespace(fetch_only=True)
		frappe_mock.get_doc.side_effect = lambda doctype, name: (
			mapping if doctype == "SAP Doctype Mapping" else row
		)
		frappe_mock.cache.return_value.set.return_value = True

		synchronize_sap_data(
			row.name, fetch_only=True, from_date="2025-01-01", to_date="2026-10-31"
		)

		get_mapped_data.assert_called_once_with(
			row.sap_mapping,
			configuration_name=row.parent,
			row_name=row.name,
			from_date="2025-01-01",
			to_date="2026-10-31",
		)

	@patch("emkan_insights.sap.tasks._has_empty_staging", return_value=True)
	@patch("emkan_insights.sap.tasks._has_staged_mapping_errors", return_value=False)
	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={"failed": []})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_empty_staging_with_cursor_fetches_history(
		self, frappe_mock, get_mapped_data, _staged_errors, _empty_staging
	):
		row = self._row()
		row.last_sync = "2026-11-03 12:00:00"
		mapping = SimpleNamespace(full_snapshot_sync=0, fetch_only=True)
		frappe_mock.get_doc.side_effect = lambda doctype, name: (
			mapping if doctype == "SAP Doctype Mapping" else row
		)
		frappe_mock.cache.return_value.set.return_value = True

		synchronize_sap_data(row.name, fetch_only=True)

		self.assertEqual(get_mapped_data.call_args.kwargs["from_date"], "2000-01-01")
		self.assertEqual(get_mapped_data.call_args.kwargs["to_date"], nowdate())

	@patch("emkan_insights.sap.tasks.synchronize_sap_data", return_value={"created": ["journal:42"]})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_review_configuration_fetches_only_enabled_fetch_only_rows(self, frappe_mock, fetch_row):
		frappe_mock.get_all.side_effect = [
			[
				SimpleNamespace(name="ROW-1", sap_mapping="SAP GL Journal Detail"),
				SimpleNamespace(name="ROW-2", sap_mapping="SAP Item"),
			],
			["SAP GL Journal Detail"],
		]

		result = fetch_sap_review_configuration("SAP - Company")

		self.assertEqual(result["results"][0]["created"], 1)
		self.assertEqual(len(result["results"]), 1)
		fetch_row.assert_called_once_with("ROW-1")

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile")
	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={
		"created": ["journal:42"], "updated": [], "failed": []
	})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_fetch_only_mapping_does_not_sync_erpnext_ledgers(
		self, frappe_mock, _get_mapped_data, sync_mapping
	):
		row = self._row()
		row.sap_mapping = "SAP GL Journal Detail"
		mapping = SimpleNamespace(fetch_only=True)
		frappe_mock.get_doc.side_effect = lambda doctype, name: (
			mapping if doctype == "SAP Doctype Mapping" else row
		)
		frappe_mock.cache.return_value.set.return_value = True

		result = synchronize_sap_data(row.name)

		self.assertTrue(result["fetch_only"])
		sync_mapping.assert_not_called()

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile")
	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={
		"created": ["ACCOUNT-1"], "updated": [], "failed": []
	})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_explicit_fetch_only_does_not_sync_accounts(self, frappe_mock, _get_mapped_data, sync_mapping):
		row = self._row()
		mapping = SimpleNamespace(fetch_only=False)
		frappe_mock.get_doc.side_effect = lambda doctype, name: (
			mapping if doctype == "SAP Doctype Mapping" else row
		)
		frappe_mock.cache.return_value.set.return_value = True

		result = synchronize_sap_data(row.name, fetch_only=True)

		self.assertTrue(result["fetch_only"])
		sync_mapping.assert_not_called()

	def test_job_id_is_stable_and_row_specific(self):
		self.assertEqual(_job_id("ROW-1"), _job_id("ROW-1"))
		self.assertNotEqual(_job_id("ROW-1"), _job_id("ROW-2"))

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile")
	@patch("emkan_insights.sap.tasks.get_mapped_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_synchronization_fetches_and_syncs_in_one_job(self, frappe_mock, get_mapped_data, sync_mapping):
		row = SimpleNamespace(
			name="ROW-1",
			parent="SAP - Company",
			parenttype="External SAP Configuration",
			parentfield="mappings",
			sap_mapping="SAP Item",
		)
		frappe_mock.get_doc.return_value = row
		frappe_mock.cache.return_value.set.return_value = True
		get_mapped_data.return_value = {"created": [], "updated": [], "failed": []}
		sync_mapping.return_value = {"status": "complete", "inserted": 1}

		result = synchronize_sap_data(row.name)

		self.assertEqual(result["failed"], [])
		self.assertEqual(result["sync"]["inserted"], 1)
		get_mapped_data.assert_called_once_with(
			"SAP Item",
			configuration_name="SAP - Company",
			row_name="ROW-1",
		)
		frappe_mock.cache.return_value.delete.assert_called_once_with("sap_master_sync:ROW-1")
		self.assertEqual(frappe_mock.log_error.call_count, 2)
		log = frappe_mock.log_error.call_args.kwargs
		self.assertEqual(log["title"], "SAP Fetch Completed: SAP Item")
		self.assertIn("Created: 0", log["message"])
		self.assertNotIn("defer_insert", log)
		frappe_mock.db.commit.assert_called()
		sync_mapping.assert_called_once_with(row)
		frappe_mock.enqueue.assert_not_called()

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile")
	@patch("emkan_insights.sap.tasks.get_mapped_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_full_snapshot_reuses_fetched_ids_for_deletion_check(
		self, frappe_mock, get_mapped_data, sync_mapping
	):
		row = self._row()
		mapping = SimpleNamespace(full_snapshot_sync=1, fetch_limit=0, sap_id_prefix="account:", fetch_only=False)
		frappe_mock.get_doc.side_effect = lambda doctype, name: (
			mapping if doctype == "SAP Doctype Mapping" else row
		)
		frappe_mock.cache.return_value.set.return_value = True
		get_mapped_data.return_value = {
			"success": ["account:100"], "excluded": ["700"], "failed": [],
		}

		synchronize_sap_data(row.name)

		sync_mapping.assert_called_once_with(
			row, remote_ids={"account:100", "account:700"}
		)

	@patch("emkan_insights.sap.tasks.frappe")
	def test_fetch_sap_data_skips_when_locked(self, frappe_mock):
		frappe_mock.get_doc.return_value = SimpleNamespace(
			name="ROW-1",
			parent="SAP - Company",
			parenttype="External SAP Configuration",
			parentfield="mappings",
			sap_mapping="SAP Item",
		)
		frappe_mock.cache.return_value.set.return_value = False

		result = fetch_sap_data("ROW-1")

		self.assertTrue(result["skipped"])
		self.assertEqual(frappe_mock.log_error.call_args.kwargs["title"], "SAP Fetch Skipped: SAP Item")
		frappe_mock.cache.return_value.delete.assert_not_called()

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile")
	@patch("emkan_insights.sap.tasks.get_mapped_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_failed_fetch_stops_before_sync(self, frappe_mock, get_mapped_data, sync_mapping):
		frappe_mock.get_doc.return_value = self._row()
		frappe_mock.cache.return_value.set.return_value = True
		frappe_mock.ValidationError = frappe.ValidationError
		frappe_mock.get_traceback.return_value = "SAP fetch failed traceback"
		get_mapped_data.return_value = {
			"created": ["A"], "updated": ["B"], "unchanged": ["D"], "failed": ["C"]
		}
		with self.assertRaisesRegex(frappe.ValidationError, "SAP fetch failed for SAP Item"):
			fetch_sap_data("ROW-1")
		log = frappe_mock.log_error.call_args.kwargs
		self.assertEqual(log["title"], "SAP Fetch Failed: SAP Item")
		sync_mapping.assert_not_called()

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile", side_effect=RuntimeError("Sync failed"))
	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={"failed": []})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_sync_error_stops_job(self, frappe_mock, _get_mapped_data, sync_mapping):
		frappe_mock.get_doc.return_value = self._row()
		frappe_mock.cache.return_value.set.return_value = True
		frappe_mock.get_traceback.return_value = "Sync failed traceback"

		with self.assertRaisesRegex(RuntimeError, "Sync failed"):
			synchronize_sap_data("ROW-1")

		sync_mapping.assert_called_once_with(frappe_mock.get_doc.return_value)
		self.assertEqual(frappe_mock.log_error.call_args.kwargs["title"], "SAP Document Sync Failed: Item")
		frappe_mock.cache.return_value.delete.assert_called_once_with("sap_master_sync:ROW-1")

	@patch("emkan_insights.sap.tasks._sync_mapping_and_reconcile", side_effect=RuntimeError("Sync failed"))
	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={"created": ["ITEM-1"], "failed": []})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_configuration_mode_reports_sync_failure_after_fetch(self, frappe_mock, _get_mapped_data, _sync):
		frappe_mock.get_doc.return_value = self._row()
		frappe_mock.cache.return_value.set.return_value = True
		frappe_mock.get_traceback.return_value = "Sync failed traceback"

		result = synchronize_sap_data("ROW-1", continue_on_sync_error=True)

		self.assertEqual(result["created"], ["ITEM-1"])
		self.assertEqual(result["sync"], {"status": "failed"})
		frappe_mock.db.rollback.assert_called_once()
		self.assertEqual(frappe_mock.log_error.call_args.kwargs["title"], "SAP Document Sync Failed: Item")

	@patch("emkan_insights.sap.tasks.get_mapped_data", side_effect=RuntimeError("Connection failed"))
	@patch("emkan_insights.sap.tasks.frappe")
	def test_logs_and_reraises_fetch_errors(self, frappe_mock, get_mapped_data):
		frappe_mock.get_doc.return_value = self._row()
		frappe_mock.get_traceback.return_value = "Connection failed traceback"
		with self.assertRaisesRegex(RuntimeError, "Connection failed"):
			fetch_sap_data("ROW-1")
		log = frappe_mock.log_error.call_args.kwargs
		self.assertEqual(log["title"], "SAP Fetch Failed: SAP Item")
		self.assertIn("Connection failed traceback", log["message"])
		self.assertNotIn("defer_insert", log)
		frappe_mock.db.commit.assert_called()
		frappe_mock.get_traceback.assert_called_once_with(with_context=False)
		frappe_mock.cache.return_value.delete.assert_called_once()

	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={"skipped": True, "reason": "Mapping disabled"})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_logs_disabled_mapping_as_skipped(self, frappe_mock, get_mapped_data):
		frappe_mock.get_doc.return_value = self._row()
		fetch_sap_data("ROW-1")
		self.assertEqual(frappe_mock.log_error.call_args.kwargs["title"], "SAP Fetch Skipped: SAP Item")

	@patch("emkan_insights.sap.tasks.frappe")
	def test_scheduler_enqueues_one_job_for_all_configurations(self, frappe_mock):
		second_row = SimpleNamespace(name="ROW-2", parent="SAP - Second", sap_mapping="SAP Account")
		frappe_mock.get_all.side_effect = [[self._row(), second_row], ["SAP Item", "SAP Account"]]
		self.assertEqual(enqueue_sap_synchronization(), {"queued": 1})
		frappe_mock.enqueue.assert_called_once()
		self.assertEqual(
			frappe_mock.enqueue.call_args.args[0],
			"emkan_insights.sap.tasks.synchronize_sap_configurations",
		)
		self.assertEqual(frappe_mock.enqueue.call_args.kwargs["queue"], "long")
		self.assertEqual(
			frappe_mock.enqueue.call_args.kwargs["configuration_names"],
			["SAP - Company", "SAP - Second"],
		)
		self.assertEqual(frappe_mock.enqueue.call_args.kwargs["job_id"], "sap_synchronize_all")

	@patch("emkan_insights.sap.tasks.synchronize_sap_configuration")
	def test_master_runs_configurations_sequentially(self, synchronize):
		synchronize.side_effect = [
			{"status": "complete", "configuration": "SAP - Company"},
			{"status": "complete", "configuration": "SAP - Second"},
		]

		result = synchronize_sap_configurations(["SAP - Company", "SAP - Second"])

		self.assertEqual(result["status"], "complete")
		self.assertEqual(result["processed_configurations"], 2)
		self.assertEqual(
			[call.args[0] for call in synchronize.call_args_list],
			["SAP - Company", "SAP - Second"],
		)

	@patch("emkan_insights.sap.tasks.synchronize_sap_configuration")
	def test_master_reports_configuration_sync_errors(self, synchronize):
		synchronize.side_effect = [
			{"status": "completed_with_errors", "configuration": "SAP - Company"},
			{"status": "complete", "configuration": "SAP - Second"},
		]
		result = synchronize_sap_configurations(["SAP - Company", "SAP - Second"])
		self.assertEqual(result["status"], "completed_with_errors")

	@patch("emkan_insights.sap.tasks.synchronize_sap_configuration")
	def test_master_stops_after_configuration_error(self, synchronize):
		synchronize.side_effect = RuntimeError("SAP unavailable")

		with self.assertRaisesRegex(RuntimeError, "SAP unavailable"):
			synchronize_sap_configurations(["SAP - Company", "SAP - Second"])

		synchronize.assert_called_once_with("SAP - Company")

	@patch("emkan_insights.sap.tasks.synchronize_sap_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_configuration_job_runs_enabled_mappings_sequentially(self, frappe_mock, synchronize):
		second_row = SimpleNamespace(name="ROW-2", sap_mapping="SAP Account")
		frappe_mock.get_all.side_effect = [[self._row(), second_row], ["SAP Item", "SAP Account"]]
		synchronize.side_effect = [
			{"created": ["ITEM-1"], "updated": [], "unchanged": [], "failed": [], "sync": {"inserted": 1}},
			{"created": [], "updated": [], "unchanged": ["ACCOUNT-1"], "failed": [], "sync": {"updated": 0}},
		]

		result = synchronize_sap_configuration("SAP - Company")

		self.assertEqual(result["status"], "complete")
		self.assertEqual(result["processed_mappings"], 2)
		self.assertEqual(
			frappe_mock.log_error.call_args_list[0].kwargs["title"],
			"SAP Synchronization Job Started: SAP - Company",
		)
		self.assertEqual([call.args[0] for call in synchronize.call_args_list], ["ROW-1", "ROW-2"])
		self.assertEqual(result["results"][0]["created"], 1)
		logger = frappe_mock.logger.return_value
		logger.setLevel.assert_called_once()
		self.assertEqual(
			logger.info.call_args_list[0].args,
			("SAP synchronization started: %s / %s", "SAP - Company", "SAP Item"),
		)
		self.assertEqual(
			logger.info.call_args_list[1].args[1:4],
			("complete", "SAP - Company", "SAP Item"),
		)
		frappe_mock.enqueue.assert_not_called()

	@patch("emkan_insights.sap.tasks.synchronize_sap_data", return_value={"created": [], "failed": []})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_configuration_fetch_passes_staging_only_to_each_row(self, frappe_mock, synchronize):
		frappe_mock.get_all.side_effect = [[self._row()], ["SAP Item"]]

		result = synchronize_sap_configuration("SAP - Company", fetch_only=True)

		self.assertEqual(result["status"], "complete")
		synchronize.assert_called_once_with("ROW-1", continue_on_sync_error=True, fetch_only=True)

	@patch("emkan_insights.sap.tasks.synchronize_sap_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_configuration_job_stops_after_one_mapping_fails(self, frappe_mock, synchronize):
		second_row = SimpleNamespace(name="ROW-2", sap_mapping="SAP Account")
		frappe_mock.get_all.side_effect = [[self._row(), second_row], ["SAP Item", "SAP Account"]]
		synchronize.side_effect = RuntimeError("SAP unavailable")

		with self.assertRaisesRegex(RuntimeError, "SAP unavailable"):
			synchronize_sap_configuration("SAP - Company")

		synchronize.assert_called_once_with("ROW-1", continue_on_sync_error=True, fetch_only=False)

	@patch("emkan_insights.sap.tasks.synchronize_sap_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_configuration_continues_after_document_sync_failure(self, frappe_mock, synchronize):
		second_row = SimpleNamespace(name="ROW-2", sap_mapping="SAP Account")
		frappe_mock.get_all.side_effect = [[self._row(), second_row], ["SAP Item", "SAP Account"]]
		synchronize.side_effect = [
			{"created": ["ITEM-1"], "failed": [], "sync": {"status": "failed"}},
			{"created": ["ACCOUNT-1"], "failed": [], "sync": {"status": "complete"}},
		]

		result = synchronize_sap_configuration("SAP - Company")

		self.assertEqual(result["status"], "completed_with_errors")
		self.assertEqual([item["status"] for item in result["results"]], ["sync_failed", "complete"])
		self.assertEqual([call.kwargs for call in synchronize.call_args_list], [
			{"continue_on_sync_error": True, "fetch_only": False},
			{"continue_on_sync_error": True, "fetch_only": False},
		])

	@patch("emkan_insights.sap.tasks.synchronize_sap_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_configuration_job_reports_skipped_locked_mapping(self, frappe_mock, synchronize):
		frappe_mock.get_all.side_effect = [[self._row()], ["SAP Item"]]
		synchronize.return_value = {"skipped": True, "reason": "A synchronization is already running."}

		result = synchronize_sap_configuration("SAP - Company")

		self.assertEqual(result["status"], "completed_with_skips")
		self.assertEqual(result["results"][0]["status"], "skipped")

	@patch("emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.frappe")
	def test_row_fetch_queues_staging_only_job(self, frappe_mock):
		frappe_mock.get_doc.return_value = self._row()

		result = fetch_mapping("ROW-1")

		self.assertEqual(result["status"], "queued")
		self.assertIs(frappe_mock.enqueue.call_args.args[0], synchronize_sap_data)
		self.assertEqual(frappe_mock.enqueue.call_args.kwargs["job_id"], _job_id("ROW-1"))
		self.assertTrue(frappe_mock.enqueue.call_args.kwargs["fetch_only"])
		self.assertNotIn("enqueue_after_commit", frappe_mock.enqueue.call_args.kwargs)

	@patch("emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.frappe")
	def test_fetch_configuration_queues_only_selected_company(self, frappe_mock):
		configuration = SimpleNamespace(name="SAP - Company", company="Company A")
		frappe_mock.get_doc.return_value = configuration

		result = fetch_configuration(configuration.name)

		self.assertEqual(result, {"status": "queued", "company": "Company A"})
		frappe_mock.has_permission.assert_called_once_with(
			"External SAP Configuration", "write", doc=configuration, throw=True
		)
		self.assertIs(frappe_mock.enqueue.call_args.args[0], synchronize_sap_configuration)
		self.assertEqual(
			frappe_mock.enqueue.call_args.kwargs["configuration_name"], configuration.name
		)
		self.assertTrue(frappe_mock.enqueue.call_args.kwargs["fetch_only"])
		self.assertEqual(
			frappe_mock.enqueue.call_args.kwargs["job_id"],
			_configuration_synchronization_job_id(configuration.name),
		)
		self.assertTrue(frappe_mock.enqueue.call_args.kwargs["deduplicate"])
		self.assertNotIn("enqueue_after_commit", frappe_mock.enqueue.call_args.kwargs)

	@patch("emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.frappe")
	def test_fetch_configuration_reports_actual_deduplicated_job(self, frappe_mock):
		frappe_mock.get_doc.return_value = SimpleNamespace(name="SAP - Company", company="Company A")
		frappe_mock.enqueue.return_value = None
		self.assertEqual(fetch_configuration("SAP - Company")["status"], "already_queued")
		self.assertNotIn("enqueue_after_commit", frappe_mock.enqueue.call_args.kwargs)

	@patch("emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.frappe")
	def test_fetch_and_sync_configuration_queues_combined_job(self, frappe_mock):
		configuration = SimpleNamespace(name="SAP - Company", company="Company A")
		frappe_mock.get_doc.return_value = configuration
		frappe_mock.get_all.return_value = ["SAP Item"]
		frappe_mock.db.exists.return_value = True

		result = fetch_and_sync_configuration(configuration.name)

		self.assertEqual(result, {"status": "queued", "company": "Company A"})
		frappe_mock.has_permission.assert_called_once_with(
			"External SAP Configuration", "write", doc=configuration, throw=True
		)
		self.assertIs(frappe_mock.enqueue.call_args.args[0], synchronize_sap_configuration)
		self.assertFalse(frappe_mock.enqueue.call_args.kwargs["fetch_only"])
		self.assertEqual(
			frappe_mock.enqueue.call_args.kwargs["job_id"],
			_configuration_synchronization_job_id(configuration.name),
		)
		self.assertTrue(frappe_mock.enqueue.call_args.kwargs["deduplicate"])

	@patch("emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.frappe")
	def test_fetch_and_sync_configuration_rejects_fetch_only_configuration(self, frappe_mock):
		configuration = SimpleNamespace(name="SAP - Company", company="Company A")
		frappe_mock.get_doc.return_value = configuration
		frappe_mock.get_all.return_value = ["SAP GL Journal Detail"]
		frappe_mock.db.exists.return_value = False
		frappe_mock.ValidationError = frappe.ValidationError

		with self.assertRaisesRegex(frappe.ValidationError, "All enabled mappings are fetch only"):
			fetch_and_sync_configuration(configuration.name)

		frappe_mock.enqueue.assert_not_called()

	@patch("emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.frappe")
	def test_manual_sync_reports_actual_job_status(self, frappe_mock):
		frappe_mock.get_doc.return_value = self._row()
		frappe_mock.enqueue.return_value = None
		self.assertEqual(queue_sync_mapping("ROW-1")["status"], "already_queued")
		self.assertNotIn("enqueue_after_commit", frappe_mock.enqueue.call_args.kwargs)

	def test_old_queued_paths_remain_callable(self):
		from emkan_insights.sap.tasks import (
			enqueue_incremental_master_fetch,
			enqueue_incremental_master_sync,
			sync_mapping_row,
		)

		self.assertIs(sync_mapping_row, fetch_sap_data)
		self.assertIs(fetch_sap_data, synchronize_sap_data)
		self.assertIs(enqueue_incremental_master_sync, enqueue_sap_synchronization)
		self.assertIs(enqueue_incremental_master_fetch, enqueue_sap_synchronization)

	@patch("emkan_insights.sap.deletion.reconcile_mapping_deletions")
	@patch("emkan_insights.sap.tasks.sync_mapping")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_sync_sap_data_bulk_syncs_then_reconciles_deletions(
		self,
		frappe_mock,
		sync_mapping,
		reconcile_deletions,
	):
		frappe_mock.get_doc.side_effect = [self._row(), SimpleNamespace(full_snapshot_sync=1)]
		frappe_mock.cache.return_value.set.return_value = True
		sync_mapping.return_value = {"status": "complete", "inserted": 2, "updated": 1}
		reconcile_deletions.return_value = {"status": "complete", "deleted": 1}

		result = sync_sap_data("ROW-1")

		sync_mapping.assert_called_once_with("ROW-1", remote_ids=None)
		reconcile_deletions.assert_called_once_with("ROW-1")
		self.assertEqual(result["deletion_reconciliation"]["deleted"], 1)
		log = frappe_mock.log_error.call_args.kwargs
		self.assertEqual(log["title"], "SAP Document Sync Completed: Item")
		self.assertIn("Inserted: 2", log["message"])
		self.assertIn("Deletion Reconciliation: complete", log["message"])
		self.assertEqual(log["reference_name"], "SAP - Company")
		frappe_mock.cache.return_value.delete.assert_called_once_with("sap_master_sync:ROW-1")

	@patch("emkan_insights.sap.deletion.reconcile_mapping_deletions")
	@patch("emkan_insights.sap.tasks.sync_mapping")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_incremental_sync_skips_full_deletion_fetch(
		self, frappe_mock, sync_mapping, reconcile_deletions
	):
		frappe_mock.get_doc.side_effect = [self._row(), SimpleNamespace(full_snapshot_sync=0)]
		frappe_mock.cache.return_value.set.return_value = True
		sync_mapping.return_value = {"status": "complete"}

		result = sync_sap_data("ROW-1")

		self.assertNotIn("deletion_reconciliation", result)
		reconcile_deletions.assert_not_called()

	@patch("emkan_insights.sap.deletion.reconcile_mapping_deletions")
	@patch("emkan_insights.sap.tasks.sync_mapping")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_limited_snapshot_skips_deletion_reconciliation(
		self, frappe_mock, sync_mapping, reconcile_deletions
	):
		frappe_mock.get_doc.side_effect = [
			self._row(), SimpleNamespace(full_snapshot_sync=1, fetch_limit=10),
		]
		frappe_mock.cache.return_value.set.return_value = True
		sync_mapping.return_value = {"status": "complete"}

		result = sync_sap_data("ROW-1")

		self.assertNotIn("deletion_reconciliation", result)
		reconcile_deletions.assert_not_called()

	@staticmethod
	def _row():
		return SimpleNamespace(
			name="ROW-1", parent="SAP - Company", parenttype="External SAP Configuration",
			parentfield="mappings", sap_mapping="SAP Item", ref_doctype="Item", exported_doctype="External Item",
		)
