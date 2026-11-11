from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.asset_acquisition import (
	_load_acquisition_amounts,
	reconcile_asset_acquisitions,
	reconcile_asset_retirements,
	submit_imported_assets,
)
from emkan_insights.sap.tasks import _sync_mapping_and_reconcile


class TestAssetAcquisition(FrappeTestCase):
	@patch("emkan_insights.sap.asset_acquisition.frappe.db.commit")
	@patch("emkan_insights.sap.asset_acquisition.frappe.db.set_value")
	@patch("emkan_insights.sap.asset_acquisition.frappe.get_all")
	@patch("emkan_insights.sap.asset_acquisition.frappe.db.get_value", return_value="Test Company")
	@patch("emkan_insights.sap.asset_acquisition.SAPB1Client.from_configuration")
	def test_posted_sale_retirement_maps_inactive_asset_to_sold(
		self, client, _company, get_all, set_value, _commit,
	):
		client.return_value.fetch_all.return_value = [
			{"Status": "adsPosted", "DocumentType": "adtSales",
				"AssetDocumentLineCollection": [{"AssetNumber": "FA-1"}]},
			{"Status": "adsCancelled", "DocumentType": "adtScrapping",
				"AssetDocumentLineCollection": [{"AssetNumber": "FA-1"}]},
		]
		get_all.return_value = [SimpleNamespace(name="FA-1", item_code="FA-1", status="Scrapped")]

		result = reconcile_asset_retirements("SAP Test", apply=True)

		self.assertEqual(result["updated"], 1)
		set_value.assert_called_once_with(
			"Asset", "FA-1", "status", "Sold", update_modified=False,
		)

	def test_posted_capitalizations_less_credit_memos(self):
		client = SimpleNamespace(fetch_all=lambda endpoint: {
			"AssetCapitalization": [
				{"DocEntry": 1, "Status": "adsPosted", "AssetDocumentLineCollection": [
					{"AssetNumber": "FA-1", "LineNumber": 1, "TotalLC": 100},
					{"AssetNumber": "FA-2", "LineNumber": 2, "TotalLC": 30},
				]},
				{"DocEntry": 2, "Status": "adsCancelled", "AssetDocumentLineCollection": [
					{"AssetNumber": "FA-1", "LineNumber": 1, "TotalLC": 900},
				]},
				{"DocEntry": 3, "Status": "adsPosted", "AssetDocumentLineCollection": [
					{"AssetNumber": "FA-1", "LineNumber": 1, "TotalLC": 20},
				]},
			],
			"AssetCapitalizationCreditMemo": [
				{"DocEntry": 4, "Status": "adsPosted", "AssetDocumentLineCollection": [
					{"AssetNumber": "FA-1", "LineNumber": 1, "TotalLC": 15},
				]},
			],
		}[endpoint])

		amounts, sources, counts = _load_acquisition_amounts(client)

		self.assertEqual(amounts, {"FA-1": Decimal("105"), "FA-2": Decimal("30")})
		self.assertEqual(sources["FA-1"], [
			"AssetCapitalization:1:1", "AssetCapitalization:3:1",
			"AssetCapitalizationCreditMemo:4:1",
		])
		self.assertEqual(counts["AssetCapitalization"], {"total": 3, "posted": 2})

	@patch("emkan_insights.sap.asset_acquisition.frappe.db.commit")
	@patch("emkan_insights.sap.asset_acquisition._update_asset_cost")
	@patch("emkan_insights.sap.asset_acquisition.frappe.get_all")
	@patch("emkan_insights.sap.asset_acquisition._load_acquisition_amounts")
	@patch("emkan_insights.sap.asset_acquisition.SAPB1Client.from_configuration")
	@patch("emkan_insights.sap.asset_acquisition.frappe.db.get_value", return_value="Test Company")
	def test_only_zero_valued_drafts_are_updated(
		self, _company, _client, load_amounts, get_all, update_cost, _commit,
	):
		load_amounts.return_value = (
			{
				"FA-1": Decimal("100"), "FA-2": Decimal("200"), "FA-3": Decimal("300"),
				"FA-4": Decimal("120"), "FA-5": Decimal("120"), "FA-6": Decimal("100"),
			},
			{f"FA-{index}": [f"cap:{index}"] for index in range(1, 7)}, {},
		)
		get_all.return_value = [
			SimpleNamespace(name="FA-1", item_code="FA-1", gross_purchase_amount=0,
				custom_sap_acquisition_amount=0, docstatus=0),
			SimpleNamespace(name="FA-2", item_code="FA-2", gross_purchase_amount=50,
				custom_sap_acquisition_amount=0, docstatus=0),
			SimpleNamespace(name="FA-3", item_code="FA-3", gross_purchase_amount=0,
				custom_sap_acquisition_amount=0, docstatus=1),
			SimpleNamespace(name="FA-4", item_code="FA-4", gross_purchase_amount=100,
				custom_sap_acquisition_amount=100, custom_sap_acquisition_sources="[]", docstatus=0),
			SimpleNamespace(name="FA-5", item_code="FA-5", gross_purchase_amount=80,
				custom_sap_acquisition_amount=100, docstatus=0),
			SimpleNamespace(name="FA-6", item_code="FA-6", gross_purchase_amount=100,
				custom_sap_acquisition_amount=0, docstatus=0),
		]

		preview = reconcile_asset_acquisitions("SAP Test")
		self.assertEqual(preview["matched"], 3)
		self.assertEqual(preview["already_valued"], 2)
		update_cost.assert_not_called()
		applied = reconcile_asset_acquisitions("SAP Test", apply=True)
		self.assertEqual(applied["updated"], 2)
		self.assertEqual(applied["provenance_recorded"], 1)
		self.assertEqual(update_cost.call_count, 3)
		update_cost.assert_any_call("FA-1", Decimal("100"), ["cap:1"])
		update_cost.assert_any_call("FA-4", Decimal("120"), ["cap:4"])

	@patch("emkan_insights.sap.tasks._log_document_sync_completed")
	@patch("emkan_insights.sap.asset_acquisition.reconcile_asset_retirements")
	@patch("emkan_insights.sap.asset_acquisition.submit_imported_assets")
	@patch("emkan_insights.sap.asset_acquisition.reconcile_asset_acquisitions")
	@patch("emkan_insights.sap.tasks.sync_mapping", return_value={"status": "complete"})
	@patch("emkan_insights.sap.tasks.frappe.get_doc")
	def test_fixed_asset_sync_refreshes_acquisition_amounts(
		self, get_doc, _sync_mapping, reconcile, submit, retire, _log,
	):
		get_doc.return_value = SimpleNamespace(
			name="SAP Fixed Asset", fetch_only=0,
			field_mapping='{"docstatus": {"fieldname": "AssetStatus", "transform": "value_map", "transform_config": {"mapping": {"Active": 1}}}}',
		)
		reconcile.return_value = {"updated": 2}
		submit.return_value = {"submitted": 2}
		retire.return_value = {"updated": 1}
		row = SimpleNamespace(name="ROW", parent="Test Configuration", sap_mapping="SAP Fixed Asset")

		result = _sync_mapping_and_reconcile(row, reconcile_deletions=False)

		self.assertEqual(result["asset_acquisition"], {"updated": 2})
		self.assertEqual(result["asset_submission"], {"submitted": 2})
		self.assertEqual(result["asset_retirement"], {"updated": 1})
		reconcile.assert_called_once_with("Test Configuration", apply=True)
		submit.assert_called_once_with("Test Configuration", apply=True)
		retire.assert_called_once_with("Test Configuration", apply=True)

	@patch("emkan_insights.sap.asset_acquisition.frappe.db.commit")
	@patch("emkan_insights.sap.asset_acquisition.frappe.db.savepoint")
	@patch("emkan_insights.sap.asset_acquisition.frappe.get_doc")
	@patch("emkan_insights.sap.asset_acquisition.frappe.get_all")
	@patch("emkan_insights.sap.asset_acquisition.frappe.db.get_value", return_value="Test Company")
	def test_submission_requires_cost_category_and_active_item(
		self, _company, get_all, get_doc, _savepoint, _commit,
	):
		get_all.side_effect = [
			[
				SimpleNamespace(name="FA-1", item_code="FA-1", asset_category="Vehicles",
					gross_purchase_amount=100, custom_sap_acquisition_amount=100, custom_sap_asset_status="Active"),
				SimpleNamespace(name="FA-2", item_code="FA-2", asset_category="Vehicles",
					gross_purchase_amount=0, custom_sap_acquisition_amount=0, custom_sap_asset_status="Active"),
				SimpleNamespace(name="FA-3", item_code="FA-3", asset_category=None,
					gross_purchase_amount=100, custom_sap_acquisition_amount=100, custom_sap_asset_status="Active"),
				SimpleNamespace(name="FA-4", item_code="FA-4", asset_category="Vehicles",
					gross_purchase_amount=100, custom_sap_acquisition_amount=100, custom_sap_asset_status="Active"),
			],
			[
				SimpleNamespace(name="FA-1", disabled=0, is_fixed_asset=1, is_stock_item=0),
				SimpleNamespace(name="FA-4", disabled=1, is_fixed_asset=1, is_stock_item=0),
			],
		]

		result = submit_imported_assets("SAP Test", apply=True)

		self.assertEqual(result["submitted"], 1)
		self.assertEqual(result["no_cost"], 1)
		self.assertEqual(result["missing_category"], 1)
		self.assertEqual(result["disabled_item"], 1)
		get_doc.assert_called_once_with("Asset", "FA-1")
		get_doc.return_value.submit.assert_called_once_with()
