# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.doctype.external_employee.external_employee import (
    sync_bulk_employees,
)


class TestExternalEmployee(FrappeTestCase):
    @patch(
        "emkan_insights.emkan_insights.doctype.external_employee.external_employee.upsert_employee"
    )
    @patch(
        "emkan_insights.emkan_insights.doctype.external_employee.external_employee._find_existing_employee",
        return_value=None,
    )
    @patch(
        "emkan_insights.emkan_insights.doctype.external_employee.external_employee.frappe.get_doc"
    )
    @patch(
        "emkan_insights.emkan_insights.doctype.external_employee.external_employee.frappe.db.get_value",
        return_value="TC",
    )
    def test_successful_sync(
        self,
        get_value,
        get_doc,
        find_existing_employee,
        upsert_employee,
    ):
        source = MagicMock(
            company="Test Company",
            modified="2026-09-01 10:00:00",
            remote_id="REMOTE-EMP-001",
        )
        source.name = "EXT-EMP-001"
        get_doc.return_value = source
        upsert_employee.return_value = MagicMock(
            name="TC-REMOTE-EMP-001",
        )

        result = sync_bulk_employees([source.name])

        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(result[0]["employee_name"], "TC-REMOTE-EMP-001")
