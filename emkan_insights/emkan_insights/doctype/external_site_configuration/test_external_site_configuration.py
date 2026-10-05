# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
	fetch_and_sync_all_companies,
)


MODULE = (
	"emkan_insights.emkan_insights.doctype.external_site_configuration."
	"external_site_configuration"
)
SCHEDULER_MODULE = (
	"emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler"
)


class TestExternalSiteConfiguration(FrappeTestCase):
	@patch(f"{SCHEDULER_MODULE}.enqueue_external_site_configuration_scheduler")
	@patch(f"{MODULE}.frappe.has_permission")
	def test_fetch_and_sync_all_companies_uses_scheduler(self, has_permission, enqueue_scheduler):
		enqueue_scheduler.return_value = {
			"status": "queued",
			"job_id": "external_site_configuration_fetch_and_sync",
		}

		result = fetch_and_sync_all_companies()

		has_permission.assert_called_once_with(
			"External Site Configuration",
			"write",
			throw=True,
		)
		enqueue_scheduler.assert_called_once_with()
		self.assertEqual(result["status"], "queued")

	def test_external_staging_doctypes_use_shared_fields(self):
		external_doctypes = frappe.get_all(
			"DocType",
			filters=[
				["module", "=", "Emkan Insights"],
				["name", "like", "External %"],
				["istable", "=", 0],
				["name", "!=", "External Site Configuration"],
			],
			pluck="name",
		)

		self.assertTrue(external_doctypes)
		for doctype in external_doctypes:
			meta = frappe.get_meta(doctype)
			standard_fields = {
				field.fieldname for field in meta.fields if not field.get("is_custom_field")
			}
			self.assertEqual(
				standard_fields,
				{
					"remote_id",
					"source_site",
					"external_sap_configuration",
					"last_synced_on",
					"json",
				},
				doctype,
			)
			self.assertEqual(meta.autoname, "hash", doctype)
			source_site = meta.get_field("source_site")
			self.assertEqual(source_site.fieldtype, "Link", doctype)
			self.assertEqual(source_site.options, "External Site Configuration", doctype)
			sap_configuration = meta.get_field("external_sap_configuration")
			self.assertEqual(sap_configuration.fieldtype, "Link", doctype)
			self.assertEqual(sap_configuration.options, "External SAP Configuration", doctype)
