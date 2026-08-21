from emkan_insights.emkan_insights.accounts import sync_account_docs
from emkan_insights.emkan_insights.external_asset_sync import sync_external_assets
from emkan_insights.emkan_insights.external_bom_sync import sync_external_bom_docs
from emkan_insights.emkan_insights.external_contact_sync import sync_external_contact_docs
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
from emkan_insights.emkan_insights.external_supplier_quotation_sync import sync_supplier_quotation_docs
from emkan_insights.emkan_insights.external_tax_category_sync import sync_tax_category_docs

from emkan_insights.emkan_insights.doctype.external_address.external_address import sync_external_records as sync_external_address_records
from emkan_insights.emkan_insights.doctype.external_asset_category.external_asset_category import sync_external_records as sync_external_asset_category_records
from emkan_insights.emkan_insights.doctype.external_bank.external_bank import sync_external_records as sync_external_bank_records
from emkan_insights.emkan_insights.doctype.external_bank_account.external_bank_account import sync_external_records as sync_external_bank_account_records
from emkan_insights.emkan_insights.doctype.external_contract.external_contract import sync_external_records as sync_external_contract_records
from emkan_insights.emkan_insights.doctype.external_customer.external_customer import sync_external_records as sync_external_customer_records
from emkan_insights.emkan_insights.doctype.external_department.external_department import sync_department_docs
from emkan_insights.emkan_insights.doctype.external_designation.external_designation import sync_designation_docs
from emkan_insights.emkan_insights.doctype.external_employee.external_employee import sync_employee_docs
from emkan_insights.emkan_insights.doctype.external_item_group.external_item_group import sync_external_records as sync_external_item_group_records
from emkan_insights.emkan_insights.doctype.external_lead.external_lead import sync_external_leads
from emkan_insights.emkan_insights.doctype.external_location.external_location import sync_external_records as sync_external_location_records
from emkan_insights.emkan_insights.doctype.external_manufacturer.external_manufacturer import sync_external_records as sync_external_manufacturer_records
from emkan_insights.emkan_insights.doctype.external_mode_of_payment.external_mode_of_payment import sync_external_docs as sync_external_mode_of_payment_docs
from emkan_insights.emkan_insights.doctype.external_opportunity.external_opportunity import sync_external_opportunities
from emkan_insights.emkan_insights.doctype.external_price_list.external_price_list import sync_external_records as sync_external_price_list_records
from emkan_insights.emkan_insights.doctype.external_production_plan.external_production_plan import sync_external_production_plan_docs
from emkan_insights.emkan_insights.doctype.external_project_type.external_project_type import sync_external_records as sync_external_project_type_records
from emkan_insights.emkan_insights.doctype.external_prospect.external_prospect import sync_external_prospects
from emkan_insights.emkan_insights.doctype.external_supplier.external_supplier import sync_external_records
from emkan_insights.emkan_insights.doctype.external_supplier_group.external_supplier_group import sync_external_records as sync_external_supplier_group_records
from emkan_insights.emkan_insights.doctype.external_territory.external_territory import sync_external_records as sync_external_territory_records
from emkan_insights.emkan_insights.doctype.external_uom.external_uom import sync_external_records as sync_external_uom_records
from emkan_insights.emkan_insights.doctype.external_warehouse.external_warehouse import sync_external_records as sync_external_warehouse_records
from emkan_insights.emkan_insights.doctype.external_work_order.external_work_order import sync_external_work_order_docs


SYNC_METHODS = {
    # Masters
    "Account": sync_account_docs,
    "Address": sync_external_address_records,
    "Asset": sync_external_assets,
    "Asset Category": sync_external_asset_category_records,
    "Bank": sync_external_bank_records,
    "Bank Account": sync_external_bank_account_records,
    "Contact": sync_external_contact_docs,
    "Contract": sync_external_contract_records,
    "Cost Center": sync_cost_center_docs,
    "Customer": sync_external_customer_records,
    "Department": sync_department_docs,
    "Designation": sync_designation_docs,
    "Employee": sync_employee_docs,
    "Item": sync_external_items,
    "Item Group": sync_external_item_group_records,
    "Item Tax Template": sync_item_tax_templates,
    "Lead": sync_external_leads,
    "Location": sync_external_location_records,
    "Manufacturer": sync_external_manufacturer_records,
    "Mode of Payment": sync_external_mode_of_payment_docs,
    "Price List": sync_external_price_list_records,
    "Project": sync_project_docs,
    "Project Type": sync_external_project_type_records,
    "Supplier": sync_external_records,
    "Supplier Group": sync_external_supplier_group_records,
    "Territory": sync_external_territory_records,
    "UOM": sync_external_uom_records,
    "Warehouse": sync_external_warehouse_records,

    # CRM
    "Prospect": sync_external_prospects,
    "Opportunity": sync_external_opportunities,
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
    "Payment Terms Template": sync_payment_template_docs,
    "Tax Category": sync_tax_category_docs,

    # Manufacturing / Stock
    "BOM": sync_external_bom_docs,
    "Production Plan": sync_external_production_plan_docs,
    "Work Order": sync_external_work_order_docs,
    "Stock Entry": sync_stock_entry_docs,
    "Stock Reconciliation": sync_stock_reconciliation_docs,
}