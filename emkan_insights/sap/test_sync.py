from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.sync import _prepare_source_row


class TestSAPSync(FrappeTestCase):
	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_prepares_stable_remote_name_and_sap_id(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname="custom_sap_id")
		source = SimpleNamespace(
			name="STAGE-1",
			remote_id=42,
			json='{"item_name":"Example"}',
		)

		result = _prepare_source_row(context, source)

		self.assertEqual(result["name"], "42")
		self.assertEqual(result["custom_sap_id"], "42")

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	def test_existing_target_name_is_preserved(self, _prepare_document_tree):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname="custom_sap_id")
		source = SimpleNamespace(name="STAGE-1", remote_id="42", json={"item_name": "Example"})

		result = _prepare_source_row(context, source, target_name="EXISTING-TARGET")

		self.assertEqual(result["name"], "EXISTING-TARGET")
