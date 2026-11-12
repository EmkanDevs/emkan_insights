import frappe

# SAP journals are append-only (a cancellation posts a new reversing journal) and have no
# update date, so fetch only journals above the highest staged JdtNum instead of all of them.
JOURNAL_MAPPINGS = ("SAP Journal Entry", "SAP GL Journal Detail")


def execute():
	frappe.reload_doc("sap", "doctype", "sap_doctype_mapping")
	for name in JOURNAL_MAPPINGS:
		if frappe.db.exists("SAP Doctype Mapping", name):
			frappe.db.set_value("SAP Doctype Mapping", name, {
				"full_snapshot_sync": 0,
				"cursor_field": "JdtNum",
				"cursor_overlap": 500,
			}, update_modified=False)
