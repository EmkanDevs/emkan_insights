from unittest.mock import MagicMock, call, patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler import (
	SCHEDULER_JOB_ID,
	enqueue_external_site_configuration_scheduler,
	run_external_site_configuration_scheduler,
)


MODULE = "emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler"


class TestExternalSiteConfigurationScheduler(FrappeTestCase):
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
	@patch(f"{MODULE}.sync_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.fetch_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.frappe.get_all")
	def test_fetches_all_configurations_before_syncing(
		self,
		get_all,
		fetch_configuration,
		sync_configuration,
		logger,
	):
		get_all.return_value = ["CPC", "Esacon"]
		events = []
		fetch_configuration.side_effect = lambda name: events.append(("fetch", name)) or {
			"status": "complete"
		}
		sync_configuration.side_effect = lambda name: events.append(("sync", name)) or {
			"status": "complete"
		}

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
		logger.return_value.info.assert_called_once()

	@patch(f"{MODULE}.frappe.log_error")
	@patch(f"{MODULE}.frappe.get_traceback", return_value="fetch error")
	@patch(f"{MODULE}.frappe.db.rollback")
	@patch(f"{MODULE}.frappe.logger")
	@patch(f"{MODULE}.sync_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.fetch_external_site_configuration_sequence_job")
	@patch(f"{MODULE}.frappe.get_all")
	def test_failed_fetch_does_not_block_other_configurations(
		self,
		get_all,
		fetch_configuration,
		sync_configuration,
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

		result = run_external_site_configuration_scheduler()

		self.assertEqual(result["status"], "complete_with_errors")
		sync_configuration.assert_called_once_with("Esacon")
		rollback.assert_called_once_with()
		log_error.assert_called_once_with(
			title="External site scheduler fetch failed: CPC",
			message="fetch error",
		)
