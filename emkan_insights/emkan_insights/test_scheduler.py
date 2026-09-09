from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.accounts import sync_account_docs
from emkan_insights.emkan_insights.scheduler import (
    _get_synced_record_count,
    _log_sync_progress,
    _synchronous_enqueue,
)


class TestSequentialSyncResultCount(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.scheduler.frappe.logger")
    @patch("emkan_insights.emkan_insights.scheduler.frappe.log_error")
    def test_progress_checkpoint_is_visible_in_error_log(self, log_error, logger):
        _log_sync_progress("Fetched Item", "Site: CPC")

        logger.return_value.info.assert_called_once_with(
            "%s | %s", "Fetched Item", "Site: CPC"
        )
        log_error.assert_called_once_with(
            title="External Sync Progress: Fetched Item",
            message="Site: CPC",
        )

    @patch("emkan_insights.emkan_insights.scheduler.frappe.db.commit")
    def test_synchronous_enqueue_drains_nested_jobs_iteratively(self, commit):
        events = []

        def chained_job(index):
            events.append(f"start-{index}")
            if index < 3:
                frappe.enqueue(chained_job, index + 1)
            events.append(f"end-{index}")

        with _synchronous_enqueue():
            frappe.enqueue(chained_job, 1)

        self.assertEqual(
            events,
            ["start-1", "end-1", "start-2", "end-2", "start-3", "end-3"],
        )
        self.assertEqual(commit.call_count, 3)

    def test_list_result_uses_actual_synced_count(self):
        count = _get_synced_record_count("Account", ["A", "B"], ["A", "B"])

        self.assertEqual(count, 2)

    def test_list_result_rejects_partial_sync(self):
        with self.assertRaises(frappe.ValidationError):
            _get_synced_record_count("Account", ["A", "B"], ["A"])

    def test_chunk_summaries_use_sum_of_success_counts(self):
        names = [f"SQ-{index}" for index in range(697)]
        result = [{"success": 300}, {"success": 300}, {"success": 97}]

        count = _get_synced_record_count("Supplier Quotation", names, result)

        self.assertEqual(count, 697)

    def test_chunk_summaries_reject_incomplete_total(self):
        names = [f"SQ-{index}" for index in range(698)]
        result = [{"success": 300}, {"success": 300}, {"success": 97}]

        with self.assertRaises(frappe.ValidationError):
            _get_synced_record_count("Supplier Quotation", names, result)

    def test_specialized_sync_uses_planned_count(self):
        count = _get_synced_record_count("Journal Entry", ["A", "B"], None)

        self.assertEqual(count, 2)

    @patch("emkan_insights.emkan_insights.accounts.sync_external_account")
    @patch("emkan_insights.emkan_insights.accounts.frappe.get_doc")
    def test_account_sync_returns_actual_target_names(self, get_doc, sync_external_account):
        get_doc.return_value = MagicMock(modified="2026-09-01 10:00:00")
        sync_external_account.side_effect = ["Account A", "Account B"]

        result = sync_account_docs("External Account", ["A", "B"], "Test Company")

        self.assertEqual(result, ["Account A", "Account B"])
