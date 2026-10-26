"""Apply field corrections verified against SAP Service Layer responses."""

import json
from pathlib import Path

import frappe

from emkan_insights.sap.review_mappings import REVIEW_MAPPINGS


MAPPING_NAMES = ("SAP Contact", "SAP Item", "SAP Project", "SAP Opportunity")


def execute():
	catalog_path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	catalog = {entry["name"]: entry for entry in json.loads(catalog_path.read_text())}

	for name in MAPPING_NAMES:
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		current = json.loads(doc.field_mapping)
		verified = catalog[name]["field_mapping"]
		if name == "SAP Contact" and current.get("department") == "Department":
			current.pop("department")
		elif name == "SAP Project" and current.get("customer") == "U_ClientCode":
			current.pop("customer")
		elif name == "SAP Item" and current.get("description") == "User_Text":
			current["description"] = verified["description"]
		elif name == "SAP Opportunity":
			for field, rule in verified.items():
				current.setdefault(field, rule)
		else:
			continue
		doc.field_mapping = json.dumps(current, indent=2)
		doc.save(ignore_permissions=True)

	for name in ("SAP Stock Counting Detail", "SAP Stock Posting Detail"):
		if not frappe.db.exists("SAP Doctype Mapping", name):
			continue
		doc = frappe.get_doc("SAP Doctype Mapping", name)
		verified = REVIEW_MAPPINGS[name]
		doc.sap_id_field = verified["sap_id_field"]
		current = json.loads(doc.field_mapping)
		current.update({
			field: rule
			for field, rule in verified["field_mapping"].items()
			if field in {"posting_date", "voucher_no", "voucher_detail_no"}
		})
		doc.field_mapping = json.dumps(current, indent=2)
		doc.save(ignore_permissions=True)
