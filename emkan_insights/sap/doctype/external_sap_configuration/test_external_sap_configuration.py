# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestExternalSAPConfiguration(FrappeTestCase):
	def test_configuration_uses_mapping_child_table(self):
		meta = frappe.get_meta("External SAP Configuration")

		self.assertIsNone(meta.get_field("external_site_configuration"))
		self.assertEqual(meta.get_field("company").options, "Company")
		self.assertTrue(meta.get_field("company").reqd)
		self.assertEqual(meta.get_field("mappings").options, "External Site Configuration CT")
		self.assertEqual(meta.get_field("session_id").fieldtype, "Password")
