from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services import _account_sync_hold_reason


class TestAccountParentHold(FrappeTestCase):
	def test_parent_with_matching_root_waits_for_hierarchy_review(self):
		parent = SimpleNamespace(root_type="Asset", is_group=1)
		with patch("frappe.db.get_value", return_value=parent):
			self.assertEqual(_account_sync_hold_reason({
				"Code": "1131101", "AccountType": "at_Other", "FatherAccountKey": "1131100",
			}, "Emkan"), "ERP Account hierarchy repair awaits approval")

	def test_parent_with_conflicting_root_is_held(self):
		parent = SimpleNamespace(root_type="Asset", is_group=1)
		with patch("frappe.db.get_value", return_value=parent):
			self.assertEqual(_account_sync_hold_reason({
				"Code": "2144110", "AccountType": "at_Other", "FatherAccountKey": "1241000",
			}, "Emkan"), "SAP Account parent has a conflicting ERP root type")
