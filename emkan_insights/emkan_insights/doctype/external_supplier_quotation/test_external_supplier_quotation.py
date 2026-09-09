# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_supplier_quotation_sync import (
	_enable_supplier_for_sync,
)


class TestExternalSupplierQuotation(FrappeTestCase):
	def test_enables_disabled_supplier_before_sync(self):
		with (
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(disabled=1, is_frozen=0),
			),
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(frappe, "clear_document_cache") as clear_cache,
		):
			changed = _enable_supplier_for_sync("SUP-0428")

		self.assertTrue(changed)
		set_value.assert_called_once_with("Supplier", "SUP-0428", {"disabled": 0})
		clear_cache.assert_called_once_with("Supplier", "SUP-0428")

	def test_unfreezes_supplier_before_sync(self):
		with (
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(disabled=0, is_frozen=1),
			),
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(frappe, "clear_document_cache") as clear_cache,
		):
			changed = _enable_supplier_for_sync("SUP-1156")

		self.assertTrue(changed)
		set_value.assert_called_once_with("Supplier", "SUP-1156", {"is_frozen": 0})
		clear_cache.assert_called_once_with("Supplier", "SUP-1156")

	def test_leaves_unrestricted_supplier_unchanged(self):
		with (
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(disabled=0, is_frozen=0),
			),
			patch.object(frappe.db, "set_value") as set_value,
		):
			changed = _enable_supplier_for_sync("SUP-0428")

		self.assertFalse(changed)
		set_value.assert_not_called()
