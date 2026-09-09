from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.fetch_window import (
    add_fetch_window_end,
    mark_fetch_completed,
    mark_fetch_started,
)


class TestFetchWindow(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.fetch_window.frappe.log_error")
    @patch("emkan_insights.emkan_insights.fetch_window.frappe.db.commit")
    @patch("emkan_insights.emkan_insights.fetch_window.frappe.db.set_value")
    def test_start_records_window_for_any_doctype(
        self, set_value, commit, log_error
    ):
        window_start, window_end = mark_fetch_started(
            "item-row",
            "Item",
            "2026-09-01 08:00:00",
            "2026-09-08 09:00:00",
        )

        set_value.assert_called_once_with(
            "External Site Configuration CT",
            "item-row",
            {
                "fetch_started_at": "2026-09-08 09:00:00.000000",
                "fetch_completed_at": None,
            },
            update_modified=False,
        )
        commit.assert_called_once()
        log_error.assert_called_once()
        self.assertEqual(window_start, "2026-09-01 08:00:00.000000")
        self.assertEqual(window_end, "2026-09-08 09:00:00.000000")

    def test_window_filter_has_fixed_upper_boundary(self):
        filters = [["modified", ">", "2026-09-01 08:00:00.000000"]]

        add_fetch_window_end(filters, "2026-09-08 09:00:00")

        self.assertEqual(
            filters[-1],
            ["modified", "<=", "2026-09-08 09:00:00.000000"],
        )

    @patch("emkan_insights.emkan_insights.fetch_window.frappe.utils.now")
    @patch("emkan_insights.emkan_insights.fetch_window.frappe.log_error")
    @patch("emkan_insights.emkan_insights.fetch_window.frappe.db.commit")
    @patch("emkan_insights.emkan_insights.fetch_window.frappe.db.set_value")
    def test_completion_advances_watermark_to_window_end(
        self, set_value, commit, log_error, now
    ):
        now.return_value = "2026-09-08 09:05:00"

        last_sync = mark_fetch_completed(
            "item-row",
            "Item",
            "2026-09-08 09:00:00",
            record_count=1250,
        )

        set_value.assert_called_once_with(
            "External Site Configuration CT",
            "item-row",
            {
                "last_sync": "2026-09-08 09:00:00.000000",
                "fetch_completed_at": "2026-09-08 09:05:00",
            },
            update_modified=False,
        )
        commit.assert_called_once()
        log_error.assert_called_once()
        self.assertEqual(last_sync, "2026-09-08 09:00:00.000000")

    def test_timestamp_fields_apply_to_every_configuration_row(self):
        meta = frappe.get_meta("External Site Configuration CT")

        for fieldname in ("fetch_started_at", "fetch_completed_at"):
            field = meta.get_field(fieldname)
            self.assertTrue(field.read_only)
            self.assertFalse(field.depends_on)
