from unittest.mock import MagicMock, call, patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.scheduler import (
	_enqueue_deletion_after_sync,
	_sync_external_configuration,
	sync_external_site_configuration_sequence_job,
)
from emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler import (
	SCHEDULER_JOB_ID,
	enqueue_external_site_configuration_scheduler,
	run_external_site_configuration_scheduler,
)


MODULE = "emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler"
SCHEDULER_MODULE = "emkan_insights.emkan_insights.scheduler"


class TestExternalSiteConfigurationScheduler(FrappeTestCase):
	@patch(
		"emkan_insights.emkan_insights.external_data_deletion."
		"enqueue_external_data_deletion_reconciliation"
	)
	@patch(
		"emkan_insights.emkan_insights.external_data_bulk_insert."
		"bulk_insert_external_data"
	)
	def test_sync_and_deletion_queue_are_separate_steps(
		self,
		bulk_insert,
		enqueue_deletions,
	):
		bulk_insert.return_value = {"status": "complete", "inserted": 2, "updated": 1}
		enqueue_deletions.return_value = {
			"status": "queued",
			"job_id": "external_data_deletion_CPC",
		}

		result = _sync_external_configuration("CPC")
		result = _enqueue_deletion_after_sync("CPC", result)

		bulk_insert.assert_called_once_with("CPC", release_lock=False)
		enqueue_deletions.assert_called_once_with("CPC", enqueue_after_commit=False)
		self.assertEqual(
			result["deletion_reconciliation"]["job_id"],
			"external_data_deletion_CPC",
		)

	@patch(f"{SCHEDULER_MODULE}._enqueue_deletion_after_sync")
	@patch(f"{SCHEDULER_MODULE}._release_site_pipeline_lock")
	@patch(f"{SCHEDULER_MODULE}._sync_external_configuration")
	@patch(f"{SCHEDULER_MODULE}._acquire_site_pipeline_lock", return_value=True)
	def test_sync_releases_lock_before_queueing_deletion(
		self,
		acquire_lock,
		sync_configuration,
		release_lock,
		enqueue_deletion,
	):
		events = []
		sync_result = {"status": "complete"}
		sync_configuration.side_effect = lambda name: events.append("sync") or sync_result
		release_lock.side_effect = lambda name: events.append("release")
		enqueue_deletion.side_effect = (
			lambda name, result: events.append("enqueue") or result
		)

		result = sync_external_site_configuration_sequence_job("CPC")

		self.assertEqual(events, ["sync", "release", "enqueue"])
		self.assertIs(result, sync_result)
		acquire_lock.assert_called_once_with("CPC")

	@patch(f"{MODULE}.frappe.enqueue")
	def test_scheduler_queues_one_deduplicated_long_job(self, enqueue):
		enqueue.return_value = MagicMock()

		result = enqueue_external_site_configuration_scheduler()

		enqueue.assert_called_once_with(
			f"{MODULE}.run_external_site_configuration_scheduler",
			queue="long",
			timeout=12 * 60 * 60,
			job_id=SCHEDULER_JOB_ID,
			deduplicate=True,
		)
		self.assertEqual(result["status"], "queued")

	@patch(f"{MODULE}.frappe.logger")
	@patch(f"{MODULE}.enqueue_external_data_deletion_batch")
	@patch(f"{MODULE}.sync_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.fetch_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.frappe.get_all")
	def test_fetches_all_configurations_before_syncing(
		self,
		get_all,
		fetch_configuration,
		sync_configuration,
		enqueue_deletion_batch,
		logger,
	):
		get_all.return_value = ["CPC", "Esacon"]
		events = []
		fetch_configuration.side_effect = lambda name: events.append(("fetch", name)) or {
			"status": "complete"
		}
		sync_configuration.side_effect = (
			lambda name, **kwargs: events.append(("sync", name))
			or {"status": "complete"}
		)
		enqueue_deletion_batch.return_value = {"status": "queued"}

		result = run_external_site_configuration_scheduler()

		self.assertEqual(
			events,
			[
				("fetch", "CPC"),
				("fetch", "Esacon"),
				("sync", "CPC"),
				("sync", "Esacon"),
			],
		)
		self.assertEqual(result["status"], "complete")
		self.assertEqual(
			sync_configuration.call_args_list,
			[
				call("CPC", enqueue_deletion=False),
				call("Esacon", enqueue_deletion=False),
			],
		)
		enqueue_deletion_batch.assert_called_once_with(
			["CPC", "Esacon"],
			enqueue_after_commit=False,
		)
		logger.return_value.info.assert_called_once()

	@patch(f"{MODULE}.frappe.log_error")
	@patch(f"{MODULE}.frappe.get_traceback", return_value="fetch error")
	@patch(f"{MODULE}.frappe.db.rollback")
	@patch(f"{MODULE}.frappe.logger")
	@patch(f"{MODULE}.enqueue_external_data_deletion_batch")
	@patch(f"{MODULE}.sync_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.fetch_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.frappe.get_all")
	def test_failed_fetch_does_not_block_other_configurations(
		self,
		get_all,
		fetch_configuration,
		sync_configuration,
		enqueue_deletion_batch,
		logger,
		rollback,
		get_traceback,
		log_error,
	):
		get_all.return_value = ["CPC", "Esacon"]
		fetch_configuration.side_effect = [
			RuntimeError("unavailable"),
			{"status": "complete"},
		]
		sync_configuration.return_value = {"status": "complete"}
		enqueue_deletion_batch.return_value = {"status": "queued"}

		result = run_external_site_configuration_scheduler()

		self.assertEqual(result["status"], "complete_with_errors")
		sync_configuration.assert_called_once_with("Esacon", enqueue_deletion=False)
		enqueue_deletion_batch.assert_called_once_with(
			["Esacon"],
			enqueue_after_commit=False,
		)
		rollback.assert_called_once_with()
		log_error.assert_called_once_with(
			title="External site scheduler fetch failed: CPC",
			message="fetch error",
		)
