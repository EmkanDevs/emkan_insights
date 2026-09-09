# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestExternalSiteConfiguration(FrappeTestCase):
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
				{"remote_id", "source_site", "last_synced_on", "json"},
				doctype,
			)
			self.assertEqual(meta.autoname, "hash", doctype)
			source_site = meta.get_field("source_site")
			self.assertEqual(source_site.fieldtype, "Link", doctype)
			self.assertEqual(source_site.options, "External Site Configuration", doctype)
