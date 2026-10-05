from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.tasks import (
	_job_id,
	enqueue_incremental_master_fetch,
	fetch_sap_data,
	sync_sap_data,
)


class TestSAPTasks(FrappeTestCase):
	def test_job_id_is_stable_and_row_specific(self):
		self.assertEqual(_job_id("ROW-1"), _job_id("ROW-1"))
		self.assertNotEqual(_job_id("ROW-1"), _job_id("ROW-2"))

	@patch("emkan_insights.sap.tasks.get_mapped_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_fetch_sap_data_uses_configuration_cursor(self, frappe_mock, get_mapped_data):
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

		result = fetch_sap_data(row.name)

		self.assertEqual(result["failed"], [])
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
		self.assertTrue(log["defer_insert"])

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

	@patch("emkan_insights.sap.tasks.get_mapped_data")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_logs_partial_failure_counts(self, frappe_mock, get_mapped_data):
		frappe_mock.get_doc.return_value = self._row()
		get_mapped_data.return_value = {"created": ["A"], "updated": ["B"], "failed": ["C"]}
		fetch_sap_data("ROW-1")
		log = frappe_mock.log_error.call_args.kwargs
		self.assertEqual(log["title"], "SAP Fetch Completed with failures: SAP Item")
		for detail in ("Processed: 3", "Created: 1", "Updated: 1", "Failed: 1", "Staging DocType: External Item"):
			self.assertIn(detail, log["message"])

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
		self.assertTrue(log["defer_insert"])
		frappe_mock.get_traceback.assert_called_once_with(with_context=False)
		frappe_mock.cache.return_value.delete.assert_called_once()

	@patch("emkan_insights.sap.tasks.get_mapped_data", return_value={"skipped": True, "reason": "Mapping disabled"})
	@patch("emkan_insights.sap.tasks.frappe")
	def test_logs_disabled_mapping_as_skipped(self, frappe_mock, get_mapped_data):
		frappe_mock.get_doc.return_value = self._row()
		fetch_sap_data("ROW-1")
		self.assertEqual(frappe_mock.log_error.call_args.kwargs["title"], "SAP Fetch Skipped: SAP Item")

	@patch("emkan_insights.sap.tasks.frappe")
	def test_scheduler_enqueues_fetch_job_for_enabled_mappings(self, frappe_mock):
		frappe_mock.get_all.side_effect = [[self._row()], ["SAP Item"]]
		self.assertEqual(enqueue_incremental_master_fetch(), {"queued": 1})
		self.assertEqual(frappe_mock.enqueue.call_args.args[0], "emkan_insights.sap.tasks.fetch_sap_data")
		self.assertEqual(frappe_mock.enqueue.call_args.kwargs["queue"], "long")
		self.assertTrue(frappe_mock.enqueue.call_args.kwargs["job_id"].startswith("sap_fetch_data_"))

	def test_old_queued_paths_remain_callable(self):
		from emkan_insights.sap.tasks import enqueue_incremental_master_sync, sync_mapping_row

		self.assertIs(sync_mapping_row, fetch_sap_data)
		self.assertIs(enqueue_incremental_master_sync, enqueue_incremental_master_fetch)

	@patch("emkan_insights.sap.deletion.reconcile_mapping_deletions")
	@patch("emkan_insights.sap.tasks.sync_mapping")
	@patch("emkan_insights.sap.tasks.frappe")
	def test_sync_sap_data_bulk_syncs_then_reconciles_deletions(
		self,
		frappe_mock,
		sync_mapping,
		reconcile_deletions,
	):
		frappe_mock.get_doc.return_value = self._row()
		frappe_mock.cache.return_value.set.return_value = True
		sync_mapping.return_value = {"status": "complete", "inserted": 2, "updated": 1}
		reconcile_deletions.return_value = {"status": "complete", "deleted": 1}

		result = sync_sap_data("ROW-1")

		sync_mapping.assert_called_once_with("ROW-1")
		reconcile_deletions.assert_called_once_with("ROW-1")
		self.assertEqual(result["deletion_reconciliation"]["deleted"], 1)
		frappe_mock.cache.return_value.delete.assert_called_once_with("sap_master_sync:ROW-1")

	@staticmethod
	def _row():
		return SimpleNamespace(
			name="ROW-1", parent="SAP - Company", parenttype="External SAP Configuration",
			parentfield="mappings", sap_mapping="SAP Item", ref_doctype="Item", exported_doctype="External Item",
		)
