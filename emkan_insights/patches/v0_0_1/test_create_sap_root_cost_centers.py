from unittest import TestCase
from unittest.mock import patch

from emkan_insights.patches.v0_0_1.create_sap_root_cost_centers import execute


class TestCreateSAPRootCostCenters(TestCase):
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.rebuild_tree")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers._get_root", return_value="Root")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.frappe")
	def test_existing_root_and_default_do_not_rebuild(self, frappe_mock, _get_root, rebuild_tree):
		frappe_mock.get_all.return_value = ["Company"]
		frappe_mock.db.get_value.side_effect = ["Default", "Root"]
		frappe_mock.db.exists.return_value = True

		execute()

		frappe_mock.db.set_value.assert_not_called()
		rebuild_tree.assert_not_called()

	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.rebuild_tree")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers._get_root", return_value="Root")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.frappe")
	def test_relinked_default_rebuilds_once(self, frappe_mock, _get_root, rebuild_tree):
		frappe_mock.get_all.return_value = ["Company"]
		frappe_mock.db.get_value.side_effect = ["Default", "Old Root"]
		frappe_mock.db.exists.return_value = True

		execute()

		frappe_mock.db.set_value.assert_called_once_with(
			"Cost Center", "Default", "parent_cost_center", "Root", update_modified=False
		)
		rebuild_tree.assert_called_once_with("Cost Center", "parent_cost_center")

	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.rebuild_tree")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.get_autoname_with_number", return_value="Root")
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers._get_root", return_value=None)
	@patch("emkan_insights.patches.v0_0_1.create_sap_root_cost_centers.frappe")
	def test_new_root_rebuilds_once(self, frappe_mock, _get_root, _autoname, rebuild_tree):
		frappe_mock.get_all.return_value = ["Company"]
		frappe_mock.db.get_value.return_value = None

		execute()

		frappe_mock.db.bulk_insert.assert_called_once()
		rebuild_tree.assert_called_once_with("Cost Center", "parent_cost_center")
