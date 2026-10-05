from types import SimpleNamespace
from unittest.mock import Mock

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.deletion import SAPDeletionError, _fetch_remote_ids, _validate_snapshot


class TestSAPDeletion(FrappeTestCase):
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

	def test_empty_snapshot_cannot_delete_existing_staging(self):
		context = SimpleNamespace(mapping_name="SAP Item")
		with self.assertRaises(SAPDeletionError):
			_validate_snapshot(context, set(), {"ITEM-1": ["STAGE-1"]})
