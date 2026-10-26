from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.accounts import sync_external_account


class TestAccountSync(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.accounts._get_company_root_account", return_value="Income - TC")
    @patch("emkan_insights.emkan_insights.accounts._find_existing_account_by_number", return_value=None)
    @patch("emkan_insights.emkan_insights.accounts._find_existing_account", return_value=None)
    @patch("emkan_insights.emkan_insights.accounts.frappe.get_meta")
    @patch("emkan_insights.emkan_insights.accounts.frappe.db.exists")
    @patch("emkan_insights.emkan_insights.accounts.frappe.db.get_value", return_value="TC")
    @patch("emkan_insights.emkan_insights.accounts.frappe.get_doc")
    def test_new_account_id_matches_account_name(
        self, get_doc, _get_value, exists, get_meta, _find_name, _find_number, _root
    ):
        account_name = "Non-Operating Income and Expenditure - 700000000000000"
        external = Mock(
            account_name=account_name,
            account_number="700000000000000",
            parent_account=None,
            root_type="Income",
            is_group=1,
            account_type=None,
            report_type="Profit and Loss",
            account_currency=None,
        )
        external.name = "External Account 1"
        external.get.return_value = "700000000000000"
        account = Mock()
        get_doc.side_effect = [external, account]
        exists.return_value = False
        get_meta.return_value.has_field.return_value = False

        result = sync_external_account("External Account 1", "Test Company")

        self.assertEqual(result, account_name)
        self.assertEqual(account.name, account_name)
        self.assertTrue(account.flags.name_set)
        account.insert.assert_called_once_with(ignore_permissions=True)

    @patch("emkan_insights.emkan_insights.accounts._get_company_root_account", return_value="Income - TC")
    @patch("emkan_insights.emkan_insights.accounts._find_existing_account_by_number", return_value=None)
    @patch("emkan_insights.emkan_insights.accounts._find_existing_account", return_value=None)
    @patch("emkan_insights.emkan_insights.accounts.frappe.get_meta")
    @patch("emkan_insights.emkan_insights.accounts.frappe.db.exists")
    @patch("emkan_insights.emkan_insights.accounts.frappe.db.get_value", return_value="TC")
    @patch("emkan_insights.emkan_insights.accounts.frappe.get_doc")
    def test_account_id_uses_company_suffix_on_collision(
        self, get_doc, _get_value, exists, get_meta, _find_name, _find_number, _root
    ):
        external = Mock(
            account_name="Cash - 1000",
            account_number="1000",
            parent_account=None,
            root_type="Asset",
            is_group=0,
            account_type=None,
            report_type="Balance Sheet",
            account_currency=None,
        )
        external.name = "External Account 1"
        external.get.return_value = "1000"
        account = Mock()
        get_doc.side_effect = [external, account]
        exists.side_effect = [True, False]
        get_meta.return_value.has_field.return_value = False

        result = sync_external_account("External Account 1", "Test Company")

        self.assertEqual(result, "Cash - 1000 - TC")
        self.assertEqual(account.name, "Cash - 1000 - TC")
