# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_po_sync import upsert_purchase_order


class TestExternalPurchaseOrder(FrappeTestCase):
	def test_enables_supplier_before_creating_purchase_order(self):
		source = SimpleNamespace(supplier="SUP-0850")

		with (
			patch(
				"emkan_insights.emkan_insights.external_po_sync.enable_supplier_for_sync",
				side_effect=RuntimeError("supplier enabled"),
			) as enable_supplier,
			patch(
				"emkan_insights.emkan_insights.external_po_sync._insert_new_po"
			) as insert_purchase_order,
			self.assertRaisesRegex(RuntimeError, "supplier enabled"),
		):
			upsert_purchase_order(source, existing_name=None)

		enable_supplier.assert_called_once_with("SUP-0850")
		insert_purchase_order.assert_not_called()
