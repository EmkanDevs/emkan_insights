"""Reconcile draft Asset costs with posted SAP capitalization documents."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import frappe

from emkan_insights.sap.services.client import SAPB1Client


CAPITALIZATION_ENDPOINTS = (
	("AssetCapitalization", 1),
	("AssetCapitalizationCreditMemo", -1),
)


def reconcile_asset_acquisitions(configuration_name: str, *, apply: bool = False) -> dict:
	"""Preview or refresh draft Asset costs from posted SAP capitalization documents."""
	company = frappe.db.get_value("External SAP Configuration", configuration_name, "company")
	if not company:
		raise frappe.ValidationError(f"SAP configuration {configuration_name} has no company.")

	client = SAPB1Client.from_configuration(configuration_name)
	amounts, sources, document_counts = _load_acquisition_amounts(client)
	assets = frappe.get_all(
		"Asset", filters={"company": company},
		fields=[
			"name", "item_code", "gross_purchase_amount", "docstatus",
			"custom_sap_acquisition_amount", "custom_sap_acquisition_sources",
		],
		limit_page_length=0,
	)
	assets_by_item = defaultdict(list)
	for asset in assets:
		assets_by_item[asset.item_code].append(asset)

	result = {
		"configuration": configuration_name,
		"company": company,
		"mode": "apply" if apply else "preview",
		"documents": document_counts,
		"matched": 0,
		"updated": 0,
		"provenance_recorded": 0,
		"already_valued": 0,
		"submitted": 0,
		"ambiguous": 0,
		"nonpositive": 0,
		"unmatched": 0,
		"no_acquisition": sum(
			len(matches) for item_code, matches in assets_by_item.items() if item_code not in amounts
		),
		"source_removed": sum(
			bool(asset.custom_sap_acquisition_amount)
			for item_code, matches in assets_by_item.items() if item_code not in amounts
			for asset in matches
		),
		"failed": [],
		"sample": [],
	}
	for item_code, amount in sorted(amounts.items()):
		matches = assets_by_item.get(item_code, [])
		if not matches:
			result["unmatched"] += 1
			continue
		if len(matches) != 1:
			result["ambiguous"] += 1
			continue
		asset = matches[0]
		if asset.docstatus != 0:
			result["submitted"] += 1
			continue
		current = Decimal(str(asset.gross_purchase_amount or 0))
		previous = Decimal(str(asset.custom_sap_acquisition_amount or 0))
		if current and current != previous and current != amount:
			result["already_valued"] += 1
			continue
		if (current == amount and previous == amount
			and asset.custom_sap_acquisition_sources == json.dumps(sources[item_code])):
			continue
		if amount <= 0:
			result["nonpositive"] += 1
			continue
		result["matched"] += 1
		if len(result["sample"]) < 10:
			result["sample"].append({
				"asset": asset.name, "amount": str(amount), "source_documents": sources[item_code]
			})
		if apply:
			try:
				_update_asset_cost(asset.name, amount, sources[item_code])
			except Exception as error:
				result["failed"].append({"asset": asset.name, "reason": str(error)[:200]})
			else:
				if current == amount:
					result["provenance_recorded"] += 1
				else:
					result["updated"] += 1
	if apply:
		frappe.db.commit()
	return result


def submit_imported_assets(configuration_name: str, *, apply: bool = False) -> dict:
	"""Submit valued draft Assets that have a posted SAP acquisition source."""
	company = frappe.db.get_value("External SAP Configuration", configuration_name, "company")
	if not company:
		raise frappe.ValidationError(f"SAP configuration {configuration_name} has no company.")
	assets = frappe.get_all(
		"Asset", filters={"company": company, "docstatus": 0},
		fields=[
			"name", "item_code", "asset_category", "gross_purchase_amount",
			"custom_sap_acquisition_amount", "custom_sap_asset_status",
		],
		limit_page_length=0,
	)
	item_codes = {asset.item_code for asset in assets}
	items = {
		item.name: item for item in frappe.get_all(
			"Item", filters={"name": ["in", sorted(item_codes)]},
			fields=["name", "disabled", "is_fixed_asset", "is_stock_item"],
			limit_page_length=0,
		)
	}
	result = {
		"configuration": configuration_name, "mode": "apply" if apply else "preview",
		"ready": 0, "submitted": 0, "no_cost": 0, "missing_category": 0,
		"disabled_item": 0, "invalid_item": 0, "manual_amount": 0,
		"not_active": 0, "failed": [],
	}
	for asset in assets:
		if asset.custom_sap_asset_status != "Active":
			result["not_active"] += 1
			continue
		cost = Decimal(str(asset.gross_purchase_amount or 0))
		source_cost = Decimal(str(asset.custom_sap_acquisition_amount or 0))
		if not cost or not source_cost:
			result["no_cost"] += 1
			continue
		if cost != source_cost:
			result["manual_amount"] += 1
			continue
		if not asset.asset_category:
			result["missing_category"] += 1
			continue
		item = items.get(asset.item_code)
		if not item or not item.is_fixed_asset or item.is_stock_item:
			result["invalid_item"] += 1
			continue
		if item.disabled:
			result["disabled_item"] += 1
			continue
		result["ready"] += 1
		if apply:
			frappe.db.savepoint("sap_asset_submit")
			try:
				frappe.get_doc("Asset", asset.name).submit()
			except Exception as error:
				frappe.db.rollback(save_point="sap_asset_submit")
				result["failed"].append({"asset": asset.name, "reason": str(error)[:200]})
			else:
				result["submitted"] += 1
	if apply:
		frappe.db.commit()
	return result


def reconcile_asset_retirements(configuration_name: str, *, apply: bool = False) -> dict:
	"""Use posted SAP retirement documents to distinguish Sold from Scrapped."""
	company = frappe.db.get_value("External SAP Configuration", configuration_name, "company")
	if not company:
		raise frappe.ValidationError(f"SAP configuration {configuration_name} has no company.")
	client = SAPB1Client.from_configuration(configuration_name)
	retired = {}
	for document in client.fetch_all("AssetRetirement"):
		if document.get("Status") != "adsPosted":
			continue
		status = {"adtSales": "Sold", "adtScrapping": "Scrapped"}.get(document.get("DocumentType"))
		if not status:
			continue
		for line in document.get("AssetDocumentLineCollection") or []:
			if item_code := line.get("AssetNumber"):
				retired[item_code] = status
	assets = frappe.get_all(
		"Asset", filters={"company": company, "custom_sap_asset_status": "Inactive"},
		fields=["name", "item_code", "status"], limit_page_length=0,
	)
	result = {"configuration": configuration_name, "mode": "apply" if apply else "preview",
		"matched": 0, "updated": 0, "without_retirement": 0}
	for asset in assets:
		status = retired.get(asset.item_code)
		if not status:
			result["without_retirement"] += 1
			continue
		result["matched"] += 1
		if apply and asset.status != status:
			frappe.db.set_value("Asset", asset.name, "status", status, update_modified=False)
			result["updated"] += 1
	if apply:
		frappe.db.commit()
	return result


def _load_acquisition_amounts(client: SAPB1Client) -> tuple[dict[str, Decimal], dict, dict]:
	amounts = defaultdict(Decimal)
	sources = defaultdict(list)
	document_counts = {}
	for endpoint, sign in CAPITALIZATION_ENDPOINTS:
		records = client.fetch_all(endpoint)
		posted = [record for record in records if record.get("Status") == "adsPosted"]
		document_counts[endpoint] = {"total": len(records), "posted": len(posted)}
		for record in posted:
			lines = record.get("AssetDocumentLineCollection")
			if not isinstance(lines, list):
				raise frappe.ValidationError(f"SAP {endpoint} {record.get('DocEntry')} has no asset lines.")
			for line in lines:
				item_code = line.get("AssetNumber")
				if not item_code:
					continue
				amount = _local_amount(line, endpoint, record.get("DocEntry"))
				amounts[str(item_code)] += sign * amount
				sources[str(item_code)].append(f"{endpoint}:{record.get('DocEntry')}:{line.get('LineNumber')}")
	return dict(amounts), {item_code: sorted(rows) for item_code, rows in sources.items()}, document_counts


def _local_amount(line: dict, endpoint: str, document_id) -> Decimal:
	value = line.get("TotalLC")
	try:
		amount = Decimal(str(value))
	except (InvalidOperation, TypeError):
		amount = None
	if amount is None or not amount.is_finite() or amount < 0:
		raise frappe.ValidationError(f"SAP {endpoint} {document_id} has an invalid TotalLC.")
	return amount


def _update_asset_cost(name: str, amount: Decimal, sources: list[str]) -> None:
	frappe.db.savepoint("sap_asset_cost")
	try:
		asset = frappe.get_doc("Asset", name)
		current = Decimal(str(asset.gross_purchase_amount or 0))
		previous = Decimal(str(asset.custom_sap_acquisition_amount or 0))
		if asset.docstatus != 0 or (current and current != previous and current != amount):
			return
		asset.gross_purchase_amount = float(amount)
		asset.custom_sap_acquisition_amount = float(amount)
		asset.custom_sap_acquisition_sources = json.dumps(sources)
		asset.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="sap_asset_cost")
		raise
