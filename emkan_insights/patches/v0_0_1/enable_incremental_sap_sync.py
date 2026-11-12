import frappe

# SAP entities that expose CreationDate/UpdateDate ($metadata verified 2026-11-12).
# SAP Opportunity exposes UpdateDate only.
INCREMENTAL_MAPPINGS = {
	"SAP Payment Purchase Down Payment Detail": ("CreationDate", "UpdateDate"),
	"SAP Payment Purchase Invoice Detail": ("CreationDate", "UpdateDate"),
	"SAP Payment Sales Invoice Detail": ("CreationDate", "UpdateDate"),
	"SAP Purchase Down Payment": ("CreationDate", "UpdateDate"),
	"SAP Stock Delivery Detail": ("CreationDate", "UpdateDate"),
	"SAP Stock Goods Issue Detail": ("CreationDate", "UpdateDate"),
	"SAP Stock Goods Receipt Detail": ("CreationDate", "UpdateDate"),
	"SAP Stock Purchase Receipt Detail": ("CreationDate", "UpdateDate"),
	"SAP Stock Transfer Detail": ("CreationDate", "UpdateDate"),
	"SAP Stock Transfer Receipt Detail": ("CreationDate", "UpdateDate"),
	"SAP Opportunity": (None, "UpdateDate"),
}


def execute():
	if not frappe.db.table_exists("SAP Doctype Mapping"):
		return
	for name, (creation_field, update_field) in INCREMENTAL_MAPPINGS.items():
		if frappe.db.exists("SAP Doctype Mapping", name):
			frappe.db.set_value("SAP Doctype Mapping", name, {
				"full_snapshot_sync": 0,
				"creation_date_filter_field": creation_field,
				"date_filter_field": update_field,
			}, update_modified=False)
