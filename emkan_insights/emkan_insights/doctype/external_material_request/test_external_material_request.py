# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_material_request_sync import (
	_resolve_local_amendment_parent,
	_resolve_optional_link,
)


class TestExternalMaterialRequest(FrappeTestCase):
	def test_resolves_company_prefixed_amendment_parent(self):
		company = "Construction Pillars Company"
		remote_name = "EHC-MAT-MR-2024-00017"
		local_name = "CPC-EHC-MAT-MR-2024-00017"

		def get_value(doctype, filters, fieldname=None):
			if doctype == "Company":
				return "CPC"
			if isinstance(filters, dict):
				return None
			if filters == local_name and fieldname == "company":
				return company
			if filters == local_name and fieldname == "docstatus":
				return 2
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "exists", side_effect=lambda _, name: name == local_name),
		):
			resolved = _resolve_local_amendment_parent(remote_name, company)

		self.assertEqual(resolved, local_name)

	def test_rejects_amendment_when_local_parent_is_not_cancelled(self):
		company = "Construction Pillars Company"
		local_name = "CPC-EHC-MAT-MR-2024-00017"

		def get_value(doctype, filters, fieldname=None):
			if doctype == "Material Request" and isinstance(filters, dict):
				return local_name
			if doctype == "Material Request" and fieldname == "docstatus":
				return 1
			return None

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			with self.assertRaises(frappe.ValidationError):
				_resolve_local_amendment_parent("EHC-MAT-MR-2024-00017", company)

	def test_optional_link_is_omitted_when_target_is_missing(self):
		with patch.object(frappe.db, "exists", return_value=False):
			self.assertIsNone(_resolve_optional_link("Letter Head", "CPC Letterhead"))
