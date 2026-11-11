import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.mapping_order import mapping_sort_key
from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.sync import _prepare_asset_locations, _skip_submitted_assets
from emkan_insights.sap.sync import _prepare_source_row


class TestFixedAssetMapping(FrappeTestCase):
	def test_fixed_assets_use_item_values_including_zero(self):
		catalog = json.loads(Path(__file__).with_name("mapping_catalog.json").read_text())
		mapping = next(entry for entry in catalog if entry["name"] == "SAP Fixed Asset")
		values = {"item_name": "Company vehicle", "asset_category": "Vehicles", "valuation_rate": 0}
		with patch("emkan_insights.sap.services.transforms.lookup") as lookup:
			lookup.side_effect = lambda value, rule, context: (
				value if rule["transform_config"]["doctype"] == "Asset Category"
				else values[rule["transform_config"]["return_field"]]
			)
			asset = Mapper(
				SimpleNamespace(erp_doctype="Asset", field_mapping=mapping["field_mapping"]),
				context={"company": "Test Company"},
			).map({"ItemCode": "FA-1", "AssetClass": "Vehicles", "AssetStatus": "Active", "CapitalizationDate": "2026-01-02"})

		self.assertEqual(asset["item_code"], "FA-1")
		self.assertEqual(asset["asset_name"], "Company vehicle")
		self.assertEqual(asset["asset_category"], "Vehicles")
		self.assertEqual(asset["gross_purchase_amount"], 0)
		self.assertEqual(asset["location"], "Test Company")
		self.assertEqual(asset["docstatus"], 1)
		self.assertEqual(asset["status"], "Submitted")
		self.assertEqual(asset["custom_sap_asset_status"], "Active")
		self.assertEqual(asset["purchase_date"], "2026-01-02")
		self.assertNotIn("_sap_mapping_errors", asset)
		fallback = Mapper(
			SimpleNamespace(erp_doctype="Asset", field_mapping=mapping["field_mapping"]),
			context={"company": "Test Company"},
		)
		with patch("emkan_insights.sap.services.transforms.lookup") as lookup:
			lookup.side_effect = lambda value, rule, context: (
				value if rule["transform_config"]["doctype"] == "Asset Category"
				else values[rule["transform_config"]["return_field"]]
			)
			asset = fallback.map({"ItemCode": "FA-1", "AssetStatus": "Inactive", "CreateDate": "2026-01-03T08:00:00"})
		self.assertEqual(asset["purchase_date"], "2026-01-03")
		self.assertEqual(asset["status"], "Scrapped")
		self.assertEqual(asset["docstatus"], 0)

	def test_category_and_item_precede_asset(self):
		ordered = sorted(
			("SAP Fixed Asset", "SAP Item", "SAP Asset Category"),
			key=lambda name: mapping_sort_key(name, 0),
		)
		self.assertEqual(ordered, ["SAP Asset Category", "SAP Item", "SAP Fixed Asset"])

	@patch("emkan_insights.sap.sync._prepare_document_tree", side_effect=lambda _context, raw: raw)
	@patch("emkan_insights.sap.sync.Mapper.fill_item_uoms")
	@patch("emkan_insights.sap.sync.frappe.db.get_value", return_value=500)
	def test_asset_stages_as_draft_without_erasing_acquisition_cost(self, _get_value, _uoms, _prepare):
		context = SimpleNamespace(bulk=object(), erp_sap_id_fieldname=None, target_doctype="Asset")
		source = SimpleNamespace(name="STAGE-1", remote_id="FA-1", json={
			"item_code": "FA-1", "gross_purchase_amount": 0, "docstatus": 1,
			"status": "Submitted", "custom_sap_asset_status": "Active",
		})

		asset = _prepare_source_row(context, source)

		self.assertEqual(asset["docstatus"], 0)
		self.assertEqual(asset["status"], "Submitted")
		self.assertEqual(asset["gross_purchase_amount"], 500)

	@patch("emkan_insights.sap.sync.frappe.get_doc")
	@patch("emkan_insights.sap.sync.frappe.db.exists", return_value=False)
	@patch("emkan_insights.sap.sync.frappe.get_all")
	@patch("emkan_insights.sap.sync.frappe.db.get_value", return_value="Test Company")
	def test_asset_sync_creates_company_location(self, _get_value, get_all, _exists, get_doc):
		get_all.side_effect = [[], [SimpleNamespace(
			name="FA-1", is_fixed_asset=1, is_stock_item=0, disabled=0,
		)]]
		context = SimpleNamespace(configuration="SAP Test")
		documents = [{"Asset": [{
			"name": "FA-1", "item_code": "FA-1", "company": "Test Company",
			"location": "Test Company", "purchase_date": "2026-01-02",
			"available_for_use_date": "2026-01-02", "gross_purchase_amount": 0,
		}]}]

		_prepare_asset_locations(context, documents)

		get_doc.assert_called_once_with({"doctype": "Location", "location_name": "Test Company"})
		get_doc.return_value.insert.assert_called_once_with(ignore_permissions=True)

	@patch("emkan_insights.sap.sync.frappe.get_all", return_value=[SimpleNamespace(
		name="FA-1", company="Test Company", status="Submitted", custom_sap_asset_status="Active",
	)])
	def test_submitted_asset_is_not_overwritten(self, _get_all):
		documents = [{"Asset": [{"name": "FA-1", "company": "Test Company", "status": "Submitted", "custom_sap_asset_status": "Active"}]}, {"Asset": [{"name": "FA-2", "company": "Test Company"}]}]
		self.assertEqual(_skip_submitted_assets(documents), ([documents[1]], 1))

	@patch("emkan_insights.sap.sync.frappe.db.set_value")
	@patch("emkan_insights.sap.sync.frappe.get_all", return_value=[SimpleNamespace(
		name="FA-1", company="Test Company", status="Submitted", custom_sap_asset_status="Active",
	)])
	def test_sap_inactive_marks_submitted_asset_scrapped(self, _get_all, set_value):
		documents = [{"Asset": [{
			"name": "FA-1", "company": "Test Company", "status": "Scrapped",
			"custom_sap_asset_status": "Inactive",
		}]}]
		self.assertEqual(_skip_submitted_assets(documents), ([], 1))
		set_value.assert_called_once_with(
			"Asset", "FA-1",
			{"custom_sap_asset_status": "Inactive", "status": "Scrapped"},
			update_modified=False,
		)
