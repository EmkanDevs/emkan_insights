from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.deletion import (
	SAPDeletionError,
	_fetch_remote_ids,
	_get_target_names,
	_validate_snapshot,
	reconcile_mapping_deletions,
)


class TestSAPDeletion(FrappeTestCase):
	@patch("emkan_insights.sap.deletion._log_result")
	@patch("emkan_insights.sap.deletion._get_staging_rows", return_value={})
	@patch("emkan_insights.sap.deletion.SAPB1Client.from_configuration")
	@patch("emkan_insights.sap.deletion.get_sync_context")
	@patch("emkan_insights.sap.deletion.frappe.get_doc")
	def test_reconciliation_uses_supplied_snapshot_without_fetching_again(
		self, get_doc, get_sync_context, client_factory, _get_staging_rows, _log_result
	):
		get_sync_context.return_value = SimpleNamespace(mapping_name="SAP Account")
		get_doc.return_value = SimpleNamespace()

		result = reconcile_mapping_deletions("ROW-1", remote_ids={"100"})

		self.assertEqual(result["remote"], 1)
		client_factory.assert_not_called()

	@patch("emkan_insights.sap.deletion.frappe.get_all")
	def test_account_deletion_finds_name_based_target(self, get_all):
		get_all.side_effect = [
			[SimpleNamespace(remote_id="1000", json='{"account_name": "Cash - 1000"}')],
			["Cash - 1000"],
		]
		context = SimpleNamespace(
			erp_sap_id_fieldname=None,
			target_doctype="Account",
			source_doctype="External Account",
			configuration="Emkan",
			bulk=SimpleNamespace(abbr_doctypes=frozenset(), company_abbr=""),
		)

		self.assertEqual(_get_target_names(context, ["1000"]), ["Cash - 1000"])
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"name": ["in", ["1000", "Cash - 1000"]]},
		)

	def test_full_snapshot_uses_mapping_filter_and_nested_rows(self):
		client = Mock()
		client.fetch_all.return_value = [
			{
				"CardCode": "C-1",
				"ContactEmployees": [{"InternalCode": 10}, {"InternalCode": 11}],
			}
		]
		mapping = SimpleNamespace(
			sap_doctype="BusinessPartners",
			sap_filter="CardType eq 'cCustomer'",
			date_filter_field="UpdateDate",
			source_array_field="ContactEmployees",
			sap_id_field="InternalCode",
		)

		self.assertEqual(_fetch_remote_ids(client, mapping), {"10", "11"})
		client.fetch_all.assert_called_once_with(
			"BusinessPartners",
			params={"$filter": "(CardType eq 'cCustomer')"},
		)

	def test_remote_ids_include_mapping_prefix(self):
		client = Mock()
		client.fetch_all.return_value = [{"DocEntry": 7}]
		mapping = SimpleNamespace(
			sap_doctype="IncomingPayments",
			sap_filter=None,
			date_filter_field=None,
			full_snapshot_sync=1,
			source_array_field=None,
			sap_id_field="DocEntry",
			sap_id_prefix="incoming:",
		)

		self.assertEqual(_fetch_remote_ids(client, mapping), {"incoming:7"})

	def test_empty_snapshot_cannot_delete_existing_staging(self):
		context = SimpleNamespace(mapping_name="SAP Item")
		with self.assertRaises(SAPDeletionError):
			_validate_snapshot(context, set(), {"ITEM-1": ["STAGE-1"]})
