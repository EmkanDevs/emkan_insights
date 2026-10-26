from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import getdate, now_datetime

from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.services.exclusions import get_excluded_sap_id_prefixes, is_excluded_sap_id
from emkan_insights.sap.services.fetch import fetch
from emkan_insights.sap.services.mapper import Mapper
from emkan_insights.sap.services.references import SAPReferenceResolver
from emkan_insights.sap.services.staging import StagingWriter


def get_mapped_data(
	mapping_name: str,
	from_date: str | None = None,
	to_date: str | None = None,
	configuration_name: str | None = None,
	row_name: str | None = None,
) -> dict[str, Any]:
	mapping_doc = frappe.get_doc("SAP Doctype Mapping", mapping_name)
	if not mapping_doc.enabled:
		return {"skipped": True, "reason": "Mapping disabled"}

	configuration = _get_configuration(mapping_name, configuration_name)
	row = _get_mapping_row(configuration.name, mapping_name, row_name)
	window_end = now_datetime()
	automatic_window = not from_date and not to_date
	if automatic_window and row and row.last_sync and not mapping_doc.full_snapshot_sync:
		from_date = getdate(row.last_sync).isoformat()
		to_date = getdate(window_end).isoformat()
	_mark_started(row, window_end)
	client = SAPB1Client.from_configuration(configuration.name)
	records = fetch(client, mapping_doc, from_date, to_date)
	mapper = None if getattr(mapping_doc, "raw_payload", False) else Mapper(
		mapping_doc,
		context={
			"company": configuration.company,
			"currency": frappe.db.get_value("Company", configuration.company, "default_currency"),
			"sap_references": SAPReferenceResolver(client),
		},
	)
	writer = StagingWriter(
		mapping_doc.external_doctype,
		configuration.name,
	)
	result = {"success": [], "created": [], "updated": [], "unchanged": [], "excluded": [], "failed": []}
	excluded_prefixes = get_excluded_sap_id_prefixes(mapping_doc)

	for index, sap_record in enumerate(records):
		raw_id = _sap_record_id(sap_record, mapping_doc.sap_id_field)
		if is_excluded_sap_id(raw_id, excluded_prefixes):
			result["excluded"].append(str(raw_id))
			if mapping_doc.name == "SAP Account":
				_process_record(
					mapping_doc, mapper, writer, sap_record, index, result,
					forced_hold_reason="SAP Account prefix needs accounting classification",
				)
			continue
		_process_record(mapping_doc, mapper, writer, sap_record, index, result)

	_mark_completed(row, window_end, result, automatic_window)
	return result


def _get_mapping_row(configuration_name: str, mapping_name: str, row_name: str | None):
	filters = {
		"parent": configuration_name,
		"parenttype": "External SAP Configuration",
		"parentfield": "mappings",
		"sap_mapping": mapping_name,
	}
	if row_name:
		filters["name"] = row_name
	row = frappe.db.get_value(
		"External Site Configuration CT",
		filters,
		["name", "last_sync"],
		as_dict=True,
	)
	if row_name and not row:
		raise frappe.ValidationError("The SAP mapping row does not belong to this configuration.")
	return row


def _mark_started(row, window_end) -> None:
	if not row:
		return
	frappe.db.set_value(
		"External Site Configuration CT",
		row.name,
		{"fetch_started_at": window_end, "fetch_completed_at": None},
		update_modified=False,
	)
	frappe.db.commit()


def _mark_completed(row, window_end, result: dict[str, Any], automatic_window: bool) -> None:
	if not row:
		return
	values = {"fetch_completed_at": now_datetime()}
	if automatic_window and not result["failed"]:
		values["last_sync"] = window_end
	frappe.db.set_value(
		"External Site Configuration CT",
		row.name,
		values,
		update_modified=False,
	)
	result["last_sync"] = values.get("last_sync")


def _get_configuration(mapping_name: str, configuration_name: str | None):
	if configuration_name:
		return frappe.get_doc("External SAP Configuration", configuration_name)

	configuration_name = frappe.db.get_value(
		"External Site Configuration CT",
		{
			"sap_mapping": mapping_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
		},
		"parent",
	)
	if not configuration_name:
		raise frappe.ValidationError(f"No External SAP Configuration contains mapping '{mapping_name}'.")
	return frappe.get_doc("External SAP Configuration", configuration_name)


def _process_record(
	mapping_doc,
	mapper: Mapper | None,
	writer: StagingWriter,
	sap_record: dict,
	index: int,
	result: dict,
	forced_hold_reason: str | None = None,
) -> None:
	raw_id = _sap_record_id(sap_record, mapping_doc.sap_id_field)
	if raw_id in (None, ""):
		raise frappe.ValidationError(
			f"SAP record #{index + 1} for {mapping_doc.name} has no {mapping_doc.sap_id_field}."
		)
	sap_id = f"{getattr(mapping_doc, 'sap_id_prefix', '') or ''}{raw_id}"

	savepoint = f"sap_sync_{index}"
	frappe.db.savepoint(savepoint)
	try:
		mapper_context = getattr(mapper, "context", None)
		company = mapper_context.get("company") if isinstance(mapper_context, dict) else None
		hold_reason = forced_hold_reason or _sync_hold_reason(mapping_doc, sap_record, company=company)
		payload = (
			{"sap_mapping": mapping_doc.name, "source_endpoint": mapping_doc.sap_doctype, "record": sap_record}
			if getattr(mapping_doc, "raw_payload", False) or hold_reason else mapper.map(sap_record)
		)
		action, _staging_name = writer.upsert(sap_id, payload, sync_hold_reason=hold_reason)
		result["success"].append(sap_id)
		result[action].append(sap_id)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		result["failed"].append(sap_id)
		frappe.log_error(
			title=f"SAP Fetch Record Failed: {mapping_doc.name} #{sap_id}"[:140],
			message=f"{exc}\n\n{frappe.get_traceback(with_context=False)}",
			defer_insert=True,
		)
		raise


def _invoice_sync_hold_reason(mapping_doc, sap_record: dict) -> str | None:
	if mapping_doc.name not in {"SAP Sales Invoice", "SAP Purchase Invoice"}:
		return None
	if sap_record.get("DocType") == "dDocument_Service":
		return "Service invoice needs an ERP item mapping"
	if any(not line.get("ItemCode") for line in sap_record.get("DocumentLines") or []):
		return "Invoice line has no SAP item code"
	if mapping_doc.name == "SAP Purchase Invoice":
		for line in sap_record.get("DocumentLines") or []:
			item_code = line["ItemCode"]
			item = frappe.db.get_value(
				"Item", item_code, ["is_purchase_item", "stock_uom"], as_dict=True
			)
			if not item or not item.is_purchase_item or not item.stock_uom:
				return "ERP item needs a purchase flag and stock UOM"
			uom = line.get("UoMCode")
			if not uom or (
				uom != item.stock_uom
				and not frappe.db.get_value(
					"UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor"
				)
			):
				return "ERP item UOM conversion is missing"
	account_type = "Payable" if mapping_doc.name == "SAP Purchase Invoice" else "Receivable"
	control_account = sap_record.get("ControlAccount")
	account = (
		frappe.db.get_value("Account", {"account_number": control_account}, "account_type")
		if control_account else None
	)
	if account != account_type:
		return f"Invoice control account needs ERP {account_type} account type"
	return None


def _account_sync_hold_reason(sap_record: dict, company: str | None) -> str | None:
	parent_code = sap_record.get("FatherAccountKey")
	if not parent_code:
		return "ERP Account hierarchy repair awaits approval"
	if not company:
		return "SAP Account parent needs a configured company"
	parent = frappe.db.get_value(
		"Account", {"account_number": parent_code, "company": company},
		["root_type", "is_group"], as_dict=True,
	)
	if not parent or not parent.is_group:
		return "SAP Account parent needs an ERP group account"
	account_type = sap_record.get("AccountType")
	code = str(sap_record.get("Code") or "")
	root_type = (
		"Income" if account_type == "at_Revenues" else
		"Expense" if account_type == "at_Expenses" else
		{"1": "Asset", "2": "Liability", "3": "Equity", "4": "Income",
		 "5": "Expense", "6": "Expense", "8": "Expense"}.get(code[:1])
	)
	if not root_type or root_type != parent.root_type:
		return "SAP Account parent has a conflicting ERP root type"
	return "ERP Account hierarchy repair awaits approval"


def _order_sync_hold_reason(mapping_doc, sap_record: dict) -> str | None:
	if mapping_doc.name == "SAP Purchase Order":
		transaction_date = sap_record.get("DocDate")
		if transaction_date:
			for line in sap_record.get("DocumentLines") or []:
				if line.get("ShipDate") and getdate(line["ShipDate"]) < getdate(transaction_date):
					return "SAP line required date precedes the order transaction date"
	if mapping_doc.name == "SAP Sales Order":
		for line in sap_record.get("DocumentLines") or []:
			item_code = line.get("ItemCode")
			uom = line.get("UoMCode")
			item = (
				frappe.db.get_value("Item", item_code, ["stock_uom", "is_sales_item"], as_dict=True)
				if item_code else None
			)
			if not item or not item.is_sales_item or not item.stock_uom:
				return "ERP sales item needs a sales flag and stock UOM"
			conversion = (
				frappe.db.get_value(
					"UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor"
				)
				if uom and uom != item.stock_uom else None
			)
			if not uom or (uom != item.stock_uom and not conversion):
				return "ERP sales item UOM conversion is missing"
	return None


def _item_group_hold_reason(mapping_doc, sap_record: dict, company: str | None = None) -> str | None:
	if mapping_doc.name == "SAP Item Group":
		group_code = sap_record.get("Number")
		for source_field in ("ExpensesAccount", "RevenuesAccount"):
			account_code = sap_record.get(source_field)
			filters = {"account_number": account_code}
			if company:
				filters["company"] = company
			if account_code and not frappe.db.get_value(
				"Account", filters, "name"
			):
				return f"SAP Item Group {source_field} needs an ERP Account"
		for row in frappe.get_all(
			"Item Default", filters={"parenttype": "Item Group", "parent": str(group_code)},
			fields=["default_warehouse"],
		):
			if row.default_warehouse and not frappe.db.exists("Warehouse", row.default_warehouse):
				return "Existing ERP Item Group default warehouse needs repair"
		return None
	if mapping_doc.name == "SAP Item":
		item_code = sap_record.get("ItemCode")
		group_code = sap_record.get("ItemsGroupCode")
		if group_code in (None, "") or not frappe.db.exists("Item Group", str(group_code)):
			return "SAP Item Group code needs an ERP Item Group record"
		asset_class = sap_record.get("AssetClass")
		if asset_class and not frappe.db.exists("Asset Category", str(asset_class)):
			return "SAP Asset Class code needs an ERP Asset Category record"
		for source_field in ("InventoryUOM", "PurchaseUnit", "SalesUnit"):
			uom = sap_record.get(source_field)
			if uom and not frappe.db.exists("UOM", uom):
				return f"SAP Item {source_field} needs an ERP UOM"
		supplier_code = sap_record.get("Mainsupplier")
		if supplier_code and not frappe.db.exists("Supplier", supplier_code):
			return "SAP Item main supplier needs an ERP Supplier"
		warehouse_code = sap_record.get("DefaultWarehouse")
		if warehouse_code and not frappe.db.exists("Warehouse", warehouse_code):
			return "SAP Item default warehouse needs an ERP Warehouse"
		item = frappe.db.get_value(
			"Item", item_code,
			["item_group", "asset_category", "stock_uom", "purchase_uom", "sales_uom"],
			as_dict=True,
		) if item_code else None
		if item:
			for field, doctype in (
				("item_group", "Item Group"), ("asset_category", "Asset Category"),
				("stock_uom", "UOM"), ("purchase_uom", "UOM"), ("sales_uom", "UOM"),
			):
				value = item.get(field)
				if value and not frappe.db.exists(doctype, value):
					return f"Existing ERP Item {field} link needs repair"
			for child_type, field, target in (
				("Item Supplier", "supplier", "Supplier"),
				("Item Default", "default_supplier", "Supplier"),
				("UOM Conversion Detail", "uom", "UOM"),
			):
				for row in frappe.get_all(child_type, filters={"parent": item_code}, fields=[field]):
					value = row.get(field)
					if value and not frappe.db.exists(target, value):
						return f"Existing ERP Item {field} link needs repair"
		return None
	if mapping_doc.name not in {
		"SAP Delivery Note", "SAP Material Request", "SAP Purchase Order", "SAP Purchase Receipt", "SAP Sales Order",
		"SAP Stock Entry Issue", "SAP Stock Entry Receipt", "SAP Stock Entry Transfer",
	}:
		return None
	for line in sap_record.get("DocumentLines") or sap_record.get("StockTransferLines") or []:
		item_code = line.get("ItemCode")
		item = frappe.db.get_value("Item", item_code, ["item_group", "stock_uom"], as_dict=True) if item_code else None
		item_group = item.item_group if item else None
		if not item_group or not frappe.db.exists("Item Group", item_group):
			return "ERP stock item needs a valid Item Group link"
		if item.stock_uom and not frappe.db.exists("UOM", item.stock_uom):
			return "ERP stock item needs a valid stock UOM link"
	return None


def _partner_link_hold_reason(mapping_doc, sap_record: dict) -> str | None:
	customer_mappings = {"SAP Customer", "SAP Delivery Note", "SAP Sales Order", "SAP Sales Invoice"}
	supplier_mappings = {
		"SAP Supplier", "SAP Blanket Order", "SAP Purchase Order", "SAP Purchase Receipt",
		"SAP Purchase Invoice",
	}
	if mapping_doc.name in customer_mappings:
		party_type, group_field, group_type = "Customer", "customer_group", "Customer Group"
	elif mapping_doc.name in supplier_mappings:
		party_type, group_field, group_type = "Supplier", "supplier_group", "Supplier Group"
	else:
		return None
	party_code = sap_record.get("CardCode")
	if not party_code:
		return f"SAP {party_type} code is missing"
	if mapping_doc.name in {"SAP Customer", "SAP Supplier"}:
		group_code = sap_record.get("GroupCode")
		group_name = "Services" if party_type == "Supplier" and str(group_code) == "102" else str(group_code)
		if group_code in (None, "") or not frappe.db.exists(group_type, group_name):
			return f"SAP {party_type} group code needs an ERP {group_type} record"
		price_list_code = sap_record.get("PriceListNum")
		if price_list_code not in (None, "", -1) and not frappe.db.exists("Price List", str(price_list_code)):
			return "SAP price list code needs an ERP Price List record"
	party = frappe.db.get_value(
		party_type, party_code, [group_field, "default_price_list", "default_currency"], as_dict=True
	)
	if party:
		for field, doctype in (
			(group_field, group_type), ("default_price_list", "Price List"),
			("default_currency", "Currency"),
		):
			value = party.get(field)
			if value and not frappe.db.exists(doctype, value):
				return f"ERP {party_type} {field} link needs repair"
	return None


def _organization_link_hold_reason(mapping_doc, sap_record: dict) -> str | None:
	if mapping_doc.name == "SAP Employee":
		title = sap_record.get("JobTitle")
		if title and not frappe.db.get_value("Designation", {"designation_name": title}, "name"):
			return "SAP Employee job title needs an ERP Designation"
		employee = frappe.db.get_value(
			"Employee", str(sap_record.get("EmployeeID")), "designation"
		)
		if employee and not frappe.db.exists("Designation", employee):
			return "Existing ERP Employee designation link needs repair"
	if mapping_doc.name == "SAP Cost Center":
		group_code = sap_record.get("GroupCode")
		if group_code and not frappe.db.exists("Cost Center", group_code):
			return "SAP Profit Center group needs an ERP Cost Center grouping rule"
	return None


def _sync_hold_reason(mapping_doc, sap_record: dict, company: str | None = None) -> str | None:
	if mapping_doc.name == "SAP Account":
		return _account_sync_hold_reason(sap_record, company)
	if mapping_doc.name == "SAP Journal Entry":
		return "ERP Journal Entry accounts and Account hierarchy need repair"
	if mapping_doc.name == "SAP Supplier Group" and str(sap_record.get("Code")) == "102":
		return "SAP Supplier group 102 already uses the ERP Services group"
	if mapping_doc.name == "SAP Blanket Order":
		agreement = frappe.db.get_value(
			"Blanket Order", str(sap_record.get("AgreementNo")),
			["customer", "blanket_order_type"], as_dict=True,
		)
		if agreement and (agreement.customer or agreement.blanket_order_type != "Purchasing"):
			return "Existing ERP Blanket Order needs purchasing direction repair"
	if mapping_doc.name in {"SAP Payment Entry Incoming", "SAP Payment Entry Outgoing"}:
		if sap_record.get("DocType") == "rAccount":
			return "SAP account payment needs an ERP general ledger mapping"
		if sap_record.get("DocType") not in {"rCustomer", "rSupplier"}:
			return "SAP payment type needs a verified ERP mapping"
		return "Payment account types, exchange rates, and allocations need verification"
	if mapping_doc.name in {"SAP Purchase Order", "SAP Sales Order"}:
		if reason := _order_sync_hold_reason(mapping_doc, sap_record):
			return reason
	if reason := _item_group_hold_reason(mapping_doc, sap_record, company):
		return reason
	if reason := _invoice_sync_hold_reason(mapping_doc, sap_record):
		return reason
	if reason := _partner_link_hold_reason(mapping_doc, sap_record):
		return reason
	if reason := _organization_link_hold_reason(mapping_doc, sap_record):
		return reason
	return None


def _sap_record_id(record: dict, id_field: str) -> str | None:
	parts = [record.get(field.strip()) for field in id_field.split("+")]
	if any(part in (None, "") for part in parts):
		return None
	return ":".join(str(part) for part in parts)
