from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.tree_sync import order_tree_sync_names


class TestTreeSync(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.tree_sync.frappe.get_meta")
    @patch("emkan_insights.emkan_insights.tree_sync.frappe.db.get_value")
    @patch("emkan_insights.emkan_insights.tree_sync.frappe.db.exists")
    def test_includes_ancestors_before_requested_records(
        self,
        exists,
        get_value,
        get_meta,
    ):
        parents = {"Root": None, "Parent": "Root", "Child": "Parent"}
        exists.side_effect = lambda doctype, name: name in parents
        get_value.side_effect = lambda doctype, name, field: parents[name]

        ordered = order_tree_sync_names(
            "External Cost Center",
            ["Child"],
            "parent_cost_center",
        )

        self.assertEqual(ordered, ["Root", "Parent", "Child"])
        get_meta.assert_not_called()

    @patch("emkan_insights.emkan_insights.tree_sync.frappe.get_meta")
    @patch("emkan_insights.emkan_insights.tree_sync.frappe.db.get_value")
    @patch("emkan_insights.emkan_insights.tree_sync.frappe.db.exists")
    def test_resolves_parent_reference_by_remote_id(
        self,
        exists,
        get_value,
        get_meta,
    ):
        exists.side_effect = lambda doctype, name: name in {"Child", "Parent Row"}
        get_meta.return_value.has_field.return_value = True

        def get_value_value(doctype, name_or_filters, field):
            if name_or_filters == "Child":
                return "parent-remote-id"
            if name_or_filters == "Parent Row":
                return None
            if name_or_filters == {"remote_id": "parent-remote-id"}:
                return "Parent Row"
            return None

        get_value.side_effect = get_value_value

        ordered = order_tree_sync_names(
            "External Warehouse",
            ["Child"],
            "parent_warehouse",
        )

        self.assertEqual(ordered, ["Parent Row", "Child"])

    @patch("emkan_insights.emkan_insights.tree_sync.frappe.db.get_value")
    @patch("emkan_insights.emkan_insights.tree_sync.frappe.db.exists")
    def test_rejects_circular_hierarchy(self, exists, get_value):
        parents = {"A": "B", "B": "A"}
        exists.side_effect = lambda doctype, name: name in parents
        get_value.side_effect = lambda doctype, name, field: parents[name]

        with self.assertRaises(frappe.ValidationError):
            order_tree_sync_names("External Account", ["A"], "parent_account")
