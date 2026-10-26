import frappe


LEDGER_REVIEW_SOURCES = (
	("SAP GL Journal Detail", "JournalEntries", "GL Entry", "External GL Entry", "JdtNum", "journal:"),
	("SAP Stock Item Warehouse Detail", "Items", "Stock Ledger Entry", "External Stock Ledger Entry", "ItemCode", "item:"),
	("SAP Stock Purchase Receipt Detail", "PurchaseDeliveryNotes", "Stock Ledger Entry", "External Stock Ledger Entry", "DocEntry", "grpo:"),
	("SAP Stock Delivery Detail", "DeliveryNotes", "Stock Ledger Entry", "External Stock Ledger Entry", "DocEntry", "delivery:"),
	("SAP Stock Goods Receipt Detail", "InventoryGenEntries", "Stock Ledger Entry", "External Stock Ledger Entry", "DocEntry", "goods-in:"),
	("SAP Stock Goods Issue Detail", "InventoryGenExits", "Stock Ledger Entry", "External Stock Ledger Entry", "DocEntry", "goods-out:"),
	("SAP Stock Transfer Detail", "StockTransfers", "Stock Ledger Entry", "External Stock Ledger Entry", "DocEntry", "transfer:"),
	("SAP Stock Posting Detail", "InventoryPostings", "Stock Ledger Entry", "External Stock Ledger Entry", "DocumentEntry", "posting:"),
	("SAP Stock Counting Detail", "InventoryCountings", "Stock Ledger Entry", "External Stock Ledger Entry", "DocumentEntry", "counting:"),
	("SAP Payment Incoming Detail", "IncomingPayments", "Payment Ledger Entry", "External Payment Ledger Entry", "DocEntry", "incoming:"),
	("SAP Payment Outgoing Detail", "VendorPayments", "Payment Ledger Entry", "External Payment Ledger Entry", "DocEntry", "outgoing:"),
	("SAP Payment Sales Invoice Detail", "Invoices", "Payment Ledger Entry", "External Payment Ledger Entry", "DocEntry", "sales-invoice:"),
	("SAP Payment Purchase Invoice Detail", "PurchaseInvoices", "Payment Ledger Entry", "External Payment Ledger Entry", "DocEntry", "purchase-invoice:"),
)


def execute():
	for name, endpoint, target, staging, id_field, id_prefix in LEDGER_REVIEW_SOURCES:
		if frappe.db.exists("SAP Doctype Mapping", name):
			continue
		frappe.get_doc({
			"doctype": "SAP Doctype Mapping",
			"mapping_name": name,
			"enabled": 1,
			"sap_doctype": endpoint,
			"erp_doctype": target,
			"external_doctype": staging,
			"sap_id_field": id_field,
			"sap_id_prefix": id_prefix,
			"full_snapshot_sync": 1,
			"fetch_only": 1,
			"raw_payload": 1,
			"fetch_limit": 20,
			"field_mapping": "{}",
		}).insert(ignore_permissions=True)

	for name in frappe.get_all("External SAP Configuration", pluck="name"):
		frappe.get_doc("External SAP Configuration", name).save(ignore_permissions=True)
