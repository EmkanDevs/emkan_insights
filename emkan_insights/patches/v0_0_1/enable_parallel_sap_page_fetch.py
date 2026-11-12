import frappe

# Mappings whose full downloads took longest in the 2026-11-12 profile. Waiting on SAP pages
# one at a time dominated; four concurrent $skip pages returned the same rows 3.5x faster.
PARALLEL_MAPPINGS = (
	"SAP Contact", "SAP Inventory Transfer Request", "SAP Item", "SAP Material Request",
	"SAP Payment Entry Incoming", "SAP Payment Entry Outgoing", "SAP Payment Outgoing Allocation Detail",
	"SAP Payment Outgoing Detail", "SAP Payment Purchase Invoice Detail", "SAP Payment Sales Invoice Detail",
	"SAP Purchase Invoice", "SAP Purchase Order", "SAP Purchase Receipt", "SAP Sales Invoice",
	"SAP Sales Order", "SAP Stock Entry Issue", "SAP Stock Entry Transfer", "SAP Stock Goods Issue Detail",
	"SAP Stock Purchase Receipt Detail", "SAP Stock Transfer Detail", "SAP Stock Transfer Receipt Detail",
	"SAP Supplier Quotation", "SAP Journal Entry", "SAP GL Journal Detail", "SAP Quotation",
	"SAP Delivery Note", "SAP Stock Entry Receipt", "SAP Stock Delivery Detail",
	"SAP Stock Goods Receipt Detail", "SAP Purchase Down Payment", "SAP Fixed Asset",
)
WORKERS = 4


def execute():
	frappe.reload_doc("sap", "doctype", "sap_doctype_mapping")
	for name in PARALLEL_MAPPINGS:
		if frappe.db.exists("SAP Doctype Mapping", name):
			frappe.db.set_value("SAP Doctype Mapping", name, "fetch_workers", WORKERS, update_modified=False)
