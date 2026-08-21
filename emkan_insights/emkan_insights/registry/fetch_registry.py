from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
    sync__docs,
)

from emkan_insights.emkan_insights.sync_leads_from_remote import sync_leads_from_remote
from emkan_insights.emkan_insights.sync_quotation_from_remote import sync_quotations_from_remote
from emkan_insights.emkan_insights.sync_material_requests_from_remote import sync_material_requests_from_remote
from emkan_insights.emkan_insights.sync_cost_centers_from_remote import sync_cost_centers_from_remote
from emkan_insights.emkan_insights.sync_purchase_receipt_from_remote import sync_purchase_receipts_from_remote
from emkan_insights.emkan_insights.sync_purchase_invoice_from_remote import sync_purchase_invoices_from_remote
from emkan_insights.emkan_insights.sync_stock_entry_from_remote import sync_stock_entries_from_remote
from emkan_insights.emkan_insights.sync_stock_reconciliation_from_remote import sync_stock_reconciliations_from_remote
from emkan_insights.emkan_insights.sync_payment_request_from_remote import sync_payment_requests_from_remote
from emkan_insights.emkan_insights.sync_request_for_quotation_from_remote import sync_request_for_quotation_from_remote
from emkan_insights.emkan_insights.sync_journal_entry_from_remote import sync_journal_entries_from_remote


SPECIAL_FETCHERS = {
    "Lead": sync_leads_from_remote,
    "Quotation": sync_quotations_from_remote,
    "Material Request": sync_material_requests_from_remote,
    "Cost Center": sync_cost_centers_from_remote,
    "Purchase Receipt": sync_purchase_receipts_from_remote,
    "Purchase Invoice": sync_purchase_invoices_from_remote,
    "Stock Entry": sync_stock_entries_from_remote,
    "Stock Reconciliation": sync_stock_reconciliations_from_remote,
    "Payment Request": sync_payment_requests_from_remote,
    "Request for Quotation": sync_request_for_quotation_from_remote,
    "Journal Entry": sync_journal_entries_from_remote,
}

DEFAULT_FETCHER = sync__docs