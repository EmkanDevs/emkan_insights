import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.ledger_fetch import (
    LEDGER_DOCTYPE_MAPPINGS,
    _build_ledger_filters,
    _fetch_ledger_page,
    process_ledger_page,
    queue_ledger_fetch,
)
from emkan_insights.emkan_insights.ledger_storage import upsert_ledger_page


class TestLedgerFetch(FrappeTestCase):
    def test_configuration_has_read_only_ledger_fetch_timestamps(self):
        meta = frappe.get_meta("External Site Configuration CT")

        for fieldname in ("fetch_started_at", "fetch_completed_at"):
            field = meta.get_field(fieldname)
            self.assertIsNotNone(field)
            self.assertEqual(field.fieldtype, "Datetime")
            self.assertTrue(field.read_only)

    @patch("emkan_insights.emkan_insights.ledger_fetch._complete_ledger_fetch")
    @patch("emkan_insights.emkan_insights.ledger_fetch._enqueue_ledger_page")
    @patch("emkan_insights.emkan_insights.ledger_fetch._log_periodic_progress")
    @patch("emkan_insights.emkan_insights.ledger_fetch.frappe.db.commit")
    @patch("emkan_insights.emkan_insights.ledger_fetch.upsert_ledger_page")
    @patch("emkan_insights.emkan_insights.ledger_fetch._fetch_ledger_page")
    def test_full_page_advances_cursor_without_carrying_previous_rows(
        self,
        fetch_page,
        upsert_page,
        commit,
        log_progress,
        enqueue_page,
        complete_fetch,
    ):
        fetch_page.return_value = [
            {"name": f"ledger-{index:04d}"} for index in range(1_000)
        ]

        result = process_ledger_page(
            site_url="https://remote.example",
            api_key="key",
            api_secret="secret",
            company_abbr="CPC",
            configuration_name="CPC",
            child_docname="ledger-row",
            ref_doctype="GL Entry",
            local_doctype="External GL Entry",
            window_start="2026-09-01 08:00:00",
            window_end="2026-09-08 09:00:00",
            cursor=None,
            fetched_count=0,
        )

        self.assertEqual(result, {"status": "running", "count": 1_000})
        self.assertEqual(enqueue_page.call_args.kwargs["cursor"], "ledger-0999")
        self.assertNotIn("rows", enqueue_page.call_args.kwargs)
        upsert_page.assert_called_once()
        commit.assert_called_once()
        log_progress.assert_called_once()
        complete_fetch.assert_not_called()

    @patch("emkan_insights.emkan_insights.ledger_fetch._log_progress")
    @patch("emkan_insights.emkan_insights.ledger_fetch._enqueue_ledger_page")
    @patch("emkan_insights.emkan_insights.ledger_fetch.frappe.db.commit")
    @patch("emkan_insights.emkan_insights.ledger_fetch.frappe.db.set_value")
    @patch("emkan_insights.emkan_insights.ledger_fetch._get_company_abbr")
    def test_queue_records_fixed_fetch_window(
        self,
        get_company_abbr,
        set_value,
        commit,
        enqueue_page,
        log_progress,
    ):
        get_company_abbr.return_value = "CPC"

        result = queue_ledger_fetch(
            site_url="https://remote.example",
            api_key="key",
            api_secret="secret",
            company="CPC Company",
            configuration_name="CPC",
            child_docname="ledger-row",
            ref_doctype="GL Entry",
            window_start="2026-09-01 08:00:00",
            window_end="2026-09-08 09:00:00",
        )

        set_value.assert_called_once_with(
            "External Site Configuration CT",
            "ledger-row",
            {
                "fetch_started_at": "2026-09-08 09:00:00.000000",
                "fetch_completed_at": None,
            },
            update_modified=False,
        )
        commit.assert_called_once()
        enqueue_page.assert_called_once()
        log_progress.assert_called_once()
        self.assertEqual(result["window_start"], "2026-09-01 08:00:00.000000")
        self.assertEqual(result["window_end"], "2026-09-08 09:00:00.000000")

    def test_fast_path_contains_fetch_only_doctypes(self):
        self.assertEqual(
            LEDGER_DOCTYPE_MAPPINGS,
            {
                "GL Entry": "External GL Entry",
                "Period Closing Voucher": "External Period Closing Voucher",
                "Stock Ledger Entry": "External Stock Ledger Entry",
                "Payment Ledger Entry": "External Payment Ledger Entry",
            },
        )

    def test_window_filters_have_fixed_end_and_cursor(self):
        filters = _build_ledger_filters(
            "2026-09-01 08:00:00",
            "2026-09-08 09:00:00",
            "abc123",
        )

        self.assertEqual(
            filters,
            [
                ["modified", ">", "2026-09-01 08:00:00.000000"],
                ["modified", "<=", "2026-09-08 09:00:00.000000"],
                ["name", ">", "abc123"],
            ],
        )

    @patch("emkan_insights.emkan_insights.ledger_fetch.requests.get")
    def test_page_fetch_requests_complete_rows(self, get):
        response = MagicMock()
        response.json.return_value = {"data": [{"name": "abc123", "debit": 10}]}
        get.return_value = response

        rows = _fetch_ledger_page(
            site_url="https://remote.example",
            api_key="key",
            api_secret="secret",
            ref_doctype="GL Entry",
            window_start="2026-09-01 08:00:00",
            window_end="2026-09-08 09:00:00",
            cursor=None,
        )

        self.assertEqual(rows, [{"name": "abc123", "debit": 10}])
        params = get.call_args.kwargs["params"]
        self.assertEqual(json.loads(params["fields"]), ["*"])
        self.assertEqual(params["order_by"], "name asc")
        response.raise_for_status.assert_called_once()

    def test_page_upsert_updates_existing_ledger_payload(self):
        company = frappe.get_all("Company", fields=["name", "abbr"], limit=1)[0]
        configuration_name = frappe.get_all(
            "External Site Configuration", pluck="name", limit=1
        )[0]
        remote_id = f"ledger-test-{frappe.generate_hash(length=8)}"
        local_name = f"{company.abbr}-{remote_id}"

        upsert_ledger_page(
            local_doctype="External GL Entry",
            rows=[
                {
                    "name": remote_id,
                    "modified": "2026-09-08 09:00:00",
                    "debit": 10,
                }
            ],
            company_abbr=company.abbr,
            configuration_name=configuration_name,
        )
        upsert_ledger_page(
            local_doctype="External GL Entry",
            rows=[
                {
                    "name": remote_id,
                    "modified": "2026-09-08 09:05:00",
                    "debit": 25,
                }
            ],
            company_abbr=company.abbr,
            configuration_name=configuration_name,
        )

        stored = frappe.db.get_value(
            "External GL Entry",
            local_name,
            ["remote_id", "source_site", "last_synced_on", "json"],
            as_dict=True,
        )
        self.assertEqual(stored.remote_id, local_name)
        self.assertEqual(stored.source_site, configuration_name)
        self.assertEqual(str(stored.last_synced_on), "2026-09-08 09:05:00")
        self.assertEqual(json.loads(stored.json)["debit"], 25)
