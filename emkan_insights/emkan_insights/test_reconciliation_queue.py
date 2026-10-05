from unittest.mock import Mock, patch

from frappe import _dict
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights import external_data_bulk_insert as bulk
from emkan_insights.emkan_insights import external_data_deletion as deletion


class TestReconciliationQueue(FrappeTestCase):
	def test_queue_runs_new_reconciler_for_all_doctypes(self):
		with patch.object(deletion.frappe, "enqueue", return_value=Mock()) as enqueue:
			deletion.enqueue_external_data_deletion_reconciliation("Remote")
		args, kwargs = enqueue.call_args
		self.assertEqual(args, ("emkan_insights.emkan_insights.reconcile_missing_records.execute",))
		self.assertEqual(kwargs["configuration_name"], "Remote")
		self.assertIsNone(kwargs["doctype"])
		self.assertFalse(kwargs["dry_run"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])

	def test_bulk_completion_queues_after_release_and_failure_does_not_queue(self):
		for release_lock, fail in ((True, False), (False, False), (True, True)):
			with self.subTest(release_lock=release_lock, fail=fail):
				events = []
				cache = Mock()
				cache.delete.side_effect = lambda key: events.append("release")
				with (
					patch.object(bulk.frappe, "get_doc", return_value=_dict(name="Remote", fetch_data=[])),
					patch.object(bulk.frappe, "cache", return_value=cache),
					patch.object(bulk.frappe, "logger"),
					patch.object(bulk.frappe, "publish_realtime", side_effect=RuntimeError("failed") if fail else None),
					patch.object(deletion, "enqueue_external_data_deletion_reconciliation", side_effect=lambda name: events.append("queue")),
				):
					if fail:
						with self.assertRaises(RuntimeError):
							bulk.bulk_insert_external_data("Remote", "Administrator", release_lock=release_lock)
					else:
						bulk.bulk_insert_external_data("Remote", "Administrator", release_lock=release_lock)
				self.assertEqual(events, ["release"] if fail else ["release", "queue"] if release_lock else [])
