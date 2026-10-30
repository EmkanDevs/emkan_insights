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


def _link(fieldname, doctype, field="name", *, company_context=False, return_field="name"):
	return {
		"fieldname": fieldname,
		"transform": "lookup",
		"transform_config": {
			"doctype": doctype,
			"field": field,
			"return_field": return_field,
			"company_context": company_context,
			"required": True,
		},
	}


def _staged_document(fieldname, doctype=None, prefix="", targets=None):
	config = {"prefix": prefix}
	if doctype:
		config["doctype"] = doctype
	if targets:
		config.update({"type_field": "InvoiceType", "targets": targets})
	return {"fieldname": fieldname, "transform": "staged_document", "transform_config": config}


def _payment_ledger_amount(source_kind, currency_kind):
	return {
		"transform": "payment_ledger_amount",
		"transform_config": {"source_kind": source_kind, "currency_kind": currency_kind},
	}


PAYMENT_REFERENCE_TARGETS = {
	"it_Invoice": {"doctype": "External Sales Invoice"},
	"it_PurchaseInvoice": {"doctype": "External Purchase Invoice"},
	"it_PurchaseDownPayment": {
		"doctype": "External Purchase Invoice",
		"prefix": "down-payment:",
	},
	"it_JournalEntry": {"doctype": "External Journal Entry"},
}

PAYMENT_REFERENCE_TYPES = {
	source_type: target["doctype"] for source_type, target in PAYMENT_REFERENCE_TARGETS.items()
}


def _payment_header(prefix, account_type, party_type):
	voucher = _staged_document("DocEntry", "External Payment Entry", prefix)
	return {
		"company": _context("company"),
		"posting_date": _date("DocDate"),
		"account_type": _constant(account_type),
		"account": _link("ControlAccount", "Account", "account_number", company_context=True),
		"party_type": _constant(party_type),
		"party": _link("CardCode", party_type),
		"project": _link("ProjectCode", "Project"),
		"voucher_type": _constant("External Payment Entry"),
		"voucher_no": voucher,
		"against_voucher_type": _constant("External Payment Entry"),
		"against_voucher_no": voucher,
		"account_currency": _link(
			"ControlAccount", "Account", "account_number",
			company_context=True, return_field="account_currency",
		),
		"amount": _payment_ledger_amount("payment", "company"),
		"amount_in_account_currency": _payment_ledger_amount("payment", "account"),
		"delinked": {"fieldname": "Cancelled", "transform": "sap_bool"},
		"remarks": "Remarks",
	}


def _payment_allocation(prefix, account_type, party_type):
	return {
		"company": _context("company"),
		"posting_date": _date("__parent_DocDate"),
		"account_type": _constant(account_type),
		"account": _link(
			"__parent_ControlAccount", "Account", "account_number", company_context=True
		),
		"party_type": _constant(party_type),
		"party": _link("__parent_CardCode", party_type),
		"project": _link("__parent_ProjectCode", "Project"),
		"voucher_type": _constant("External Payment Entry"),
		"voucher_no": _staged_document(
			"__parent_DocEntry", "External Payment Entry", prefix
		),
		"against_voucher_type": {
			"fieldname": "InvoiceType",
			"transform": "value_map",
			"transform_config": {"mapping": PAYMENT_REFERENCE_TYPES, "strict": True},
		},
		"against_voucher_no": _staged_document(
			"DocEntry", targets=PAYMENT_REFERENCE_TARGETS
		),
		"voucher_detail_no": "LineNum",
		"account_currency": _link(
			"__parent_ControlAccount", "Account", "account_number",
			company_context=True, return_field="account_currency",
		),
		"amount": _payment_ledger_amount("allocation", "company"),
		"amount_in_account_currency": _payment_ledger_amount("allocation", "account"),
		"delinked": {"fieldname": "__parent_Cancelled", "transform": "sap_bool"},
		"remarks": "__parent_Remarks",
	}


def _invoice_payment(staging_doctype, prefix, account_type, party_type):
	voucher = _staged_document("DocEntry", staging_doctype, prefix)
	return {
		"company": _context("company"),
		"posting_date": _date("DocDate"),
		"due_date": _date("DocDueDate"),
		"account_type": _constant(account_type),
		"account": _link("ControlAccount", "Account", "account_number", company_context=True),
		"party_type": _constant(party_type),
		"party": _link("CardCode", party_type),
		"project": _link("Project", "Project"),
		"voucher_type": _constant(staging_doctype),
		"voucher_no": voucher,
		"against_voucher_type": _constant(staging_doctype),
		"against_voucher_no": voucher,
		"account_currency": _link(
			"ControlAccount", "Account", "account_number",
			company_context=True, return_field="account_currency",
		),
		"amount": _payment_ledger_amount("invoice", "company"),
		"amount_in_account_currency": _payment_ledger_amount("invoice", "account"),
		"delinked": {"fieldname": "Cancelled", "transform": "sap_bool"},
		"remarks": "JournalMemo",
	}


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

REVIEW_MAPPINGS = {
	"SAP GL Journal Detail": {
		"source_array": "JournalEntryLines",
		"sap_filter": None,
		"sap_id_field": "JdtNum+Line_ID",
		"field_mapping": {
			"company": _context("company"),
			"posting_date": _date("ReferenceDate"),
			"transaction_date": _date("TaxDate"),
			"account": {
				"fieldname": "AccountCode", "transform": "lookup",
				"transform_config": {
					"doctype": "Account", "field": "account_number",
					"company_context": True, "required": True,
				},
			},
			"against": "ContraAccount",
			"cost_center": {"fieldname": "CostingCode", "transform": "journal_cost_center"},
			"debit": "Debit",
			"credit": "Credit",
			"debit_in_account_currency": _account_amount("FCDebit", "Debit"),
			"credit_in_account_currency": _account_amount("FCCredit", "Credit"),
			"account_currency": _account_currency(),
			"party_type": {"fieldname": "ShortName", "transform": "journal_party"},
			"party": {"fieldname": "ShortName", "transform": "journal_party"},
			"project": "ProjectCode",
			"remarks": "LineMemo",
			"voucher_type": _constant("External Journal Entry"),
			"voucher_no": {"fieldname": "JdtNum", "transform": "journal_staged_voucher"},
			"voucher_subtype": "OriginalJournal",
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
		"field_mapping": _payment_header("incoming:", "Receivable", "Customer"),
	},
	"SAP Payment Outgoing Detail": {
		"sap_filter": "DocType eq 'rSupplier'",
		"field_mapping": _payment_header("outgoing:", "Payable", "Supplier"),
	},
	"SAP Payment Incoming Allocation Detail": {
		"source_array": "PaymentInvoices",
		"sap_filter": "DocType eq 'rCustomer'",
		"sap_id_field": "__parent_DocEntry+__line_index",
		"field_mapping": _payment_allocation("incoming:", "Receivable", "Customer"),
	},
	"SAP Payment Outgoing Allocation Detail": {
		"source_array": "PaymentInvoices",
		"sap_filter": "DocType eq 'rSupplier'",
		"sap_id_field": "__parent_DocEntry+__line_index",
		"field_mapping": _payment_allocation("outgoing:", "Payable", "Supplier"),
	},
	"SAP Payment Sales Invoice Detail": {
		"field_mapping": _invoice_payment(
			"External Sales Invoice", "", "Receivable", "Customer"
		),
	},
	"SAP Payment Purchase Invoice Detail": {
		"field_mapping": _invoice_payment(
			"External Purchase Invoice", "", "Payable", "Supplier"
		),
	},
	"SAP Payment Purchase Down Payment Detail": {
		"field_mapping": _invoice_payment(
			"External Purchase Invoice", "down-payment:", "Payable", "Supplier"
		),
	},
}
