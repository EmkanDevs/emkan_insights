import json
from pathlib import Path

import frappe


CATALOG_PATH = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
UPDATED_FIELDS = (
	"field_mapping",
	"creation_date_filter_field",
	"date_filter_field",
	"full_snapshot_sync",
	"sap_id_prefix",
)


def execute():
	for mapping in json.loads(CATALOG_PATH.read_text(encoding="utf-8")):
		values = {**mapping, "field_mapping": json.dumps(mapping["field_mapping"], ensure_ascii=False)}
		if frappe.db.exists("SAP Doctype Mapping", mapping["name"]):
			doc = frappe.get_doc("SAP Doctype Mapping", mapping["name"])
			for field in UPDATED_FIELDS:
				setattr(doc, field, values.get(field))
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc({"doctype": "SAP Doctype Mapping", **values}).insert(
				ignore_permissions=True
			)
