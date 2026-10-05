from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_sync import (
    _get_source_configuration_company,
    _sync_child_tables,
    _sync_external_docs_worker,
)


class TestExternalSyncWorker(FrappeTestCase):
    @patch("emkan_insights.emkan_insights.external_sync.frappe.db.get_value")
    def test_gets_company_from_sap_configuration(self, get_value):
        get_value.return_value = "Sap Company"

        company = _get_source_configuration_company(
            {
                "external_sap_configuration": "SAP - Sap Company",
                "source_site": None,
            }
        )

        self.assertEqual(company, "Sap Company")
        get_value.assert_called_once_with(
            "External SAP Configuration",
            "SAP - Sap Company",
            "company",
        )

    def test_tax_templates_use_generic_sync(self):
        from emkan_insights.emkan_insights.external_sync import sync_external_docs
        from emkan_insights.emkan_insights.registry.sync_registry import SYNC_METHODS

        self.assertIs(
            SYNC_METHODS["Sales Taxes and Charges Template"],
            sync_external_docs,
        )
        self.assertIs(
            SYNC_METHODS["Purchase Taxes and Charges Template"],
            sync_external_docs,
        )

    @patch("emkan_insights.emkan_insights.external_sync.resolve_company_account")
    @patch("emkan_insights.emkan_insights.external_sync.frappe.get_meta")
    def test_resolves_child_account_for_target_company(
        self,
        get_meta,
        resolve_company_account,
    ):
        table_field = MagicMock(
            fieldtype="Table",
            fieldname="taxes",
            options="Sales Taxes and Charges",
        )
        account_field = MagicMock(
            fieldname="account_head",
            fieldtype="Link",
            options="Account",
        )
        get_meta.return_value.fields = [account_field]

        source_row = MagicMock()
        source_row.as_dict.return_value = {
            "account_head": "2144103 - Output Vat 15% - IMC",
        }
        source = MagicMock()
        source.get.return_value = [source_row]

        target_row = MagicMock()
        target = MagicMock(company="Incharge Company")
        target.get.return_value = "Incharge Company"
        target.append.return_value = target_row

        resolve_company_account.return_value = (
            "2144103 - Value Added Tax 15% Collected - IMC"
        )

        _sync_child_tables(
            source,
            target,
            MagicMock(fields=[table_field]),
            company_abbr="IMC",
        )

        resolve_company_account.assert_called_once_with(
            "2144103 - Output Vat 15% - IMC",
            "Incharge Company",
            "IMC",
        )
        target_row.set.assert_called_once_with(
            "account_head",
            "2144103 - Value Added Tax 15% Collected - IMC",
        )

    @patch("emkan_insights.emkan_insights.external_sync._mark_external_docs_synced")
    @patch("emkan_insights.emkan_insights.external_sync.frappe.db.savepoint")
    @patch("emkan_insights.emkan_insights.external_sync.sync_doc_by_key")
    @patch("emkan_insights.emkan_insights.external_sync.order_tree_sync_names")
    @patch("emkan_insights.emkan_insights.external_sync.frappe.get_meta")
    def test_excludes_automatically_added_ancestors_from_results(
        self,
        get_meta,
        order_tree_sync_names,
        sync_doc_by_key,
        _savepoint,
        mark_external_docs_synced,
    ):
        get_meta.return_value = MagicMock(is_tree=1, nsm_parent_field="parent_warehouse")
        order_tree_sync_names.return_value = ["Parent Warehouse", "Requested Warehouse"]
        sync_doc_by_key.side_effect = [
            {"name": "Parent Warehouse - CPC"},
            {"name": "Requested Warehouse - CPC"},
        ]

        result = _sync_external_docs_worker(
            source_doctype="External Warehouse",
            target_doctype="Warehouse",
            names=["Requested Warehouse"],
        )

        self.assertEqual(result, ["Requested Warehouse - CPC"])
        self.assertEqual(sync_doc_by_key.call_count, 2)
        mark_external_docs_synced.assert_called_once_with(
            "External Warehouse",
            ["Parent Warehouse", "Requested Warehouse"],
        )

    @patch("emkan_insights.emkan_insights.external_sync._mark_external_docs_synced")
    @patch("emkan_insights.emkan_insights.external_sync._safe_log_error")
    @patch("emkan_insights.emkan_insights.external_sync._safe_log_external_sync_error")
    @patch("emkan_insights.emkan_insights.external_sync.frappe.db.rollback")
    @patch("emkan_insights.emkan_insights.external_sync.frappe.db.savepoint")
    @patch("emkan_insights.emkan_insights.external_sync.sync_doc_by_key")
    @patch("emkan_insights.emkan_insights.external_sync.frappe.get_meta")
    def test_stamps_only_successfully_synced_external_records(
        self,
        get_meta,
        sync_doc_by_key,
        _savepoint,
        _rollback,
        _log_external_error,
        _log_error,
        mark_external_docs_synced,
    ):
        get_meta.return_value = MagicMock(is_tree=0, nsm_parent_field=None)
        sync_doc_by_key.side_effect = [
            {"name": "SUP-0001"},
            ValueError("Invalid supplier"),
        ]

        result = _sync_external_docs_worker(
            source_doctype="External Supplier",
            target_doctype="Supplier",
            names=["EXT-SUP-0001", "EXT-SUP-0002", "EXT-SUP-0003"],
        )

        self.assertEqual(result, ["SUP-0001"])
        mark_external_docs_synced.assert_called_once_with(
            "External Supplier",
            ["EXT-SUP-0001"],
        )
