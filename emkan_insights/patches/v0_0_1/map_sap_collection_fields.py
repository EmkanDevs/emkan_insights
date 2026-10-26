import json
from pathlib import Path

import frappe


MAPPING_NAMES = {
	"SAP Customer",
	"SAP Supplier",
	"SAP Payment Entry Incoming",
	"SAP Payment Entry Outgoing",
	"SAP Journal Entry",
	"SAP Material Request",
	"SAP Supplier Quotation",
	"SAP Purchase Order",
	"SAP Purchase Receipt",
	"SAP Purchase Invoice",
	"SAP Sales Order",
	"SAP Sales Invoice",
	"SAP Stock Entry Issue",
	"SAP Quotation",
	"SAP Delivery Note",
	"SAP Stock Entry Receipt",
	"SAP Stock Entry Transfer",
	"SAP Inventory Transfer Request",
	"SAP Stock Reconciliation",
	"SAP Inventory Posting",
	"SAP Work Order",
	"SAP Blanket Order",
	"SAP Designation",
}


def execute():
	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
	for entry in catalog:
		if entry["name"] not in MAPPING_NAMES:
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", entry["name"])
		current = json.loads(doc.field_mapping)
		if _add_missing_fields(current, entry["field_mapping"]):
			doc.field_mapping = json.dumps(current, ensure_ascii=False)
			doc.save(ignore_permissions=True)


def _add_missing_fields(current, expected):
	changed = False
	for fieldname, rule in expected.items():
		if fieldname not in current:
			current[fieldname] = rule
			changed = True
		elif fieldname == "docstatus" and _needs_status_update(current[fieldname], rule):
			current[fieldname] = rule
			changed = True
		elif fieldname == "items" and isinstance(rule, dict) and "mapping" in rule:
			changed |= _add_missing_fields(current[fieldname]["mapping"], rule["mapping"])
	return changed


def _needs_status_update(current, expected):
	if not isinstance(current, dict) or not isinstance(expected, dict):
		return False
	if current.get("transform") == "conditional_value":
		conditions = current.get("transform_config", {}).get("conditions", [])
		return any(
			condition.get("when", {}).get("DocumentStatus") == ["bost_Open", "bost_Close"]
			for condition in conditions
		)
	if current.get("transform") == "value_map":
		old_values = current.get("transform_config", {}).get("mapping", {})
		new_values = expected.get("transform_config", {}).get("mapping", {})
		return old_values.get("asTerminated") == 2 and new_values.get("asTerminated") == 1
	return False
