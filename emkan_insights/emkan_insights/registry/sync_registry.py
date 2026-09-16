from emkan_insights.emkan_insights.accounts import sync_account_docs
from emkan_insights.emkan_insights.external_asset_sync import sync_external_assets
from emkan_insights.emkan_insights.external_bom_sync import sync_external_bom_docs
from emkan_insights.emkan_insights.external_cost_center_sync import sync_cost_center_docs
from emkan_insights.emkan_insights.external_delivery_note_sync import sync_external_delivery_note_docs
from emkan_insights.emkan_insights.external_item_sync import sync_external_items
from emkan_insights.emkan_insights.external_item_tax_template_sync import sync_item_tax_templates
from emkan_insights.emkan_insights.external_journal_entry_sync import sync_external_journal_entries
from emkan_insights.emkan_insights.external_material_request_sync import sync_material_request_docs
from emkan_insights.emkan_insights.external_payment_entry_sync import sync_payment_entry_docs
from emkan_insights.emkan_insights.external_payment_request_sync import sync_payment_request_docs
from emkan_insights.emkan_insights.external_payment_term_template_sync import sync_payment_template_docs
from emkan_insights.emkan_insights.external_pi_sync import sync_external_purchase_invoice_docs
from emkan_insights.emkan_insights.external_po_sync import sync_purchase_order_docs
from emkan_insights.emkan_insights.external_pr_sync import sync_external_purchase_receipt_docs
from emkan_insights.emkan_insights.external_project_sync import sync_project_docs
from emkan_insights.emkan_insights.external_quotation_sync import sync_external_quotation_docs
from emkan_insights.emkan_insights.external_rfq_sync import sync_rfq_docs
from emkan_insights.emkan_insights.external_sales_order_sync import sync_external_sales_order_docs
from emkan_insights.emkan_insights.external_si_sync import sync_external_sales_invoice_docs
from emkan_insights.emkan_insights.external_stock_entry_sync import sync_stock_entry_docs
from emkan_insights.emkan_insights.external_stock_reconciliation_sync import sync_stock_reconciliation_docs
from emkan_insights.emkan_insights.external_sync import sync_external_docs
from emkan_insights.emkan_insights.external_supplier_quotation_sync import sync_supplier_quotation_docs
from emkan_insights.emkan_insights.external_tax_category_sync import sync_tax_category_docs

SYNC_METHODS = {
    # Masters
    "Account": sync_account_docs,
    "Asset": sync_external_assets,
    "Cost Center": sync_cost_center_docs,
    "Item": sync_external_items,
    "Item Tax Template": sync_item_tax_templates,
    "Project": sync_project_docs,

    # CRM
    "Quotation": sync_external_quotation_docs,

    # Buying
    "Material Request": sync_material_request_docs,
    "Request for Quotation": sync_rfq_docs,
    "Supplier Quotation": sync_supplier_quotation_docs,
    "Purchase Order": sync_purchase_order_docs,
    "Purchase Receipt": sync_external_purchase_receipt_docs,
    "Purchase Invoice": sync_external_purchase_invoice_docs,
    "Payment Request": sync_payment_request_docs,

    # Selling
    "Sales Order": sync_external_sales_order_docs,
    "Delivery Note": sync_external_delivery_note_docs,
    "Sales Invoice": sync_external_sales_invoice_docs,

    # Accounts
    "Payment Entry": sync_payment_entry_docs,
    "Journal Entry": sync_external_journal_entries,
    "Payment Term": sync_external_docs,
    "Payment Terms Template": sync_payment_template_docs,
    "Tax Category": sync_tax_category_docs,

    # Manufacturing / Stock
    "BOM": sync_external_bom_docs,
    "Stock Entry": sync_stock_entry_docs,
    "Stock Reconciliation": sync_stock_reconciliation_docs,
    "Purchase Taxes and Charges Template": sync_external_docs,
    "Sales Taxes and Charges Template": sync_external_docs,
}
