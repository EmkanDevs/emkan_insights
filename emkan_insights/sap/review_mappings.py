"""ERPNext-shaped field mappings for fetch-only SAP ledger review sources."""


def _context(key):
	return {"transform": "context", "transform_config": {"key": key}}


def _constant(value):
	return {"transform": "constant", "transform_config": {"value": value}}


def _date(fieldname):
	return {"fieldname": fieldname, "transform": "sap_date"}


def _quantity(factor):
	return {"fieldname": "Quantity", "transform": "multiply", "transform_config": {"factor": factor}}


def _account_amount(foreign_field, base_field):
	return {"fieldname": foreign_field, "transform": "sap_account_amount", "transform_config": {"base_field": base_field}}


def _account_currency():
	return {"fieldname": "FCCurrency", "transform": "sap_account_currency"}


STOCK_LINE = {
	"company": _context("company"),
	"posting_date": _date("DocDate"),
	"item_code": "ItemCode",
	"warehouse": "WarehouseCode",
	"voucher_no": "DocNum",
	"voucher_detail_no": "LineNum",
	"project": "ProjectCode",
	"stock_uom": "UoMCode",
}

PAYMENT_HEADER = {
	"company": _context("company"),
	"posting_date": _date("DocDate"),
	"account": "ControlAccount",
	"party": "CardCode",
	"account_currency": "DocCurrency",
	"voucher_type": _constant("Payment Entry"),
	"voucher_no": "DocNum",
	"project": "ProjectCode",
	"remarks": "Remarks",
}

INVOICE_PAYMENT = {
	"company": _context("company"),
	"posting_date": _date("DocDate"),
	"account": "ControlAccount",
	"party": "CardCode",
	"account_currency": "DocCurrency",
	"voucher_no": "DocNum",
}

REVIEW_MAPPINGS = {
	"SAP GL Journal Detail": {
		"source_array": "JournalEntryLines",
		"sap_filter": "OriginalJournal ne 'ttJournalEntry'",
		"sap_id_field": "JdtNum+Line_ID",
		"field_mapping": {
			"company": _context("company"),
			"posting_date": _date("ReferenceDate"),
			"transaction_date": _date("TaxDate"),
			"account": "AccountCode",
			"against": "ContraAccount",
			"cost_center": "CostingCode",
			"debit": "Debit",
			"credit": "Credit",
			"debit_in_account_currency": _account_amount("FCDebit", "Debit"),
			"credit_in_account_currency": _account_amount("FCCredit", "Credit"),
			"account_currency": _account_currency(),
			"project": "ProjectCode",
			"remarks": "LineMemo",
			"voucher_type": "OriginalJournal",
			"voucher_no": "BaseReference",
			"voucher_detail_no": "Line_ID",
		},
	},
	"SAP Stock Item Warehouse Detail": {
		"enabled": 0,
		"source_array": "ItemWarehouseInfoCollection",
		"field_mapping": {
			"company": _context("company"),
			"item_code": "ItemCode",
			"warehouse": "WarehouseCode",
			"qty_after_transaction": "InStock",
			"valuation_rate": "StandardAveragePrice",
		},
	},
	"SAP Stock Purchase Receipt Detail": {
		"source_array": "DocumentLines",
		"sap_id_field": "DocEntry+LineNum",
		"field_mapping": {**STOCK_LINE, "actual_qty": _quantity(1), "incoming_rate": "StockInmPrice", "voucher_type": _constant("Purchase Receipt")},
	},
	"SAP Stock Delivery Detail": {
		"source_array": "DocumentLines",
		"sap_id_field": "DocEntry+LineNum",
		"field_mapping": {**STOCK_LINE, "actual_qty": _quantity(-1), "voucher_type": _constant("Delivery Note")},
	},
	"SAP Stock Goods Receipt Detail": {
		"source_array": "DocumentLines",
		"sap_id_field": "DocEntry+LineNum",
		"field_mapping": {**STOCK_LINE, "actual_qty": _quantity(1), "incoming_rate": "StockInmPrice", "voucher_type": _constant("Stock Entry")},
	},
	"SAP Stock Goods Issue Detail": {
		"source_array": "DocumentLines",
		"sap_id_field": "DocEntry+LineNum",
		"field_mapping": {**STOCK_LINE, "actual_qty": _quantity(-1), "voucher_type": _constant("Stock Entry")},
	},
	"SAP Stock Transfer Detail": {
		"source_array": "StockTransferLines",
		"sap_id_field": "DocEntry+LineNum",
		"field_mapping": {**STOCK_LINE, "warehouse": "FromWarehouseCode", "actual_qty": _quantity(-1), "voucher_type": _constant("Stock Entry")},
	},
	"SAP Stock Transfer Receipt Detail": {
		"source_array": "StockTransferLines",
		"sap_id_field": "DocEntry+LineNum",
		"field_mapping": {**STOCK_LINE, "actual_qty": _quantity(1), "voucher_type": _constant("Stock Entry")},
	},
	"SAP Stock Posting Detail": {
		"enabled": 0,
		"source_array": "InventoryPostingLines",
		"sap_id_field": "DocumentEntry+LineNumber",
		"field_mapping": {
			**STOCK_LINE,
			"posting_date": _date("PostingDate"),
			"voucher_no": "DocumentNumber",
			"voucher_detail_no": "LineNumber",
			"voucher_type": _constant("Stock Reconciliation"),
		},
	},
	"SAP Stock Counting Detail": {
		"enabled": 0,
		"source_array": "InventoryCountingLines",
		"sap_id_field": "DocumentEntry+LineNumber",
		"field_mapping": {
			**STOCK_LINE,
			"posting_date": _date("CountDate"),
			"voucher_no": "DocumentNumber",
			"voucher_detail_no": "LineNumber",
			"voucher_type": _constant("Stock Reconciliation"),
		},
	},
	"SAP Payment Incoming Detail": {
		"sap_filter": "DocType eq 'rCustomer'",
		"field_mapping": {**PAYMENT_HEADER, "account_type": _constant("Receivable"), "party_type": _constant("Customer")},
	},
	"SAP Payment Outgoing Detail": {
		"sap_filter": "DocType eq 'rSupplier'",
		"field_mapping": {**PAYMENT_HEADER, "account_type": _constant("Payable"), "party_type": _constant("Supplier")},
	},
	"SAP Payment Incoming Allocation Detail": {
		"source_array": "PaymentInvoices",
		"sap_filter": "DocType eq 'rCustomer'",
		"sap_id_field": "__parent_DocEntry+__line_index",
		"field_mapping": {
			**PAYMENT_HEADER,
			"posting_date": _date("__parent_DocDate"),
			"account": "__parent_ControlAccount",
			"party": "__parent_CardCode",
			"account_currency": "__parent_DocCurrency",
			"voucher_no": "__parent_DocNum",
			"project": "__parent_ProjectCode",
			"account_type": _constant("Receivable"),
			"party_type": _constant("Customer"),
			"against_voucher_type": "InvoiceType",
			"against_voucher_no": "DocEntry",
			"voucher_detail_no": "__line_index",
			"amount": "SumApplied",
		},
	},
	"SAP Payment Outgoing Allocation Detail": {
		"source_array": "PaymentInvoices",
		"sap_filter": "DocType eq 'rSupplier'",
		"sap_id_field": "__parent_DocEntry+__line_index",
		"field_mapping": {
			**PAYMENT_HEADER,
			"posting_date": _date("__parent_DocDate"),
			"account": "__parent_ControlAccount",
			"party": "__parent_CardCode",
			"account_currency": "__parent_DocCurrency",
			"voucher_no": "__parent_DocNum",
			"project": "__parent_ProjectCode",
			"account_type": _constant("Payable"),
			"party_type": _constant("Supplier"),
			"against_voucher_type": "InvoiceType",
			"against_voucher_no": "DocEntry",
			"voucher_detail_no": "__line_index",
			"amount": "SumApplied",
		},
	},
	"SAP Payment Sales Invoice Detail": {
		"field_mapping": {**INVOICE_PAYMENT, "account_type": _constant("Receivable"), "party_type": _constant("Customer"), "voucher_type": _constant("Sales Invoice")},
	},
	"SAP Payment Purchase Invoice Detail": {
		"field_mapping": {**INVOICE_PAYMENT, "account_type": _constant("Payable"), "party_type": _constant("Supplier"), "voucher_type": _constant("Purchase Invoice")},
	},
}
