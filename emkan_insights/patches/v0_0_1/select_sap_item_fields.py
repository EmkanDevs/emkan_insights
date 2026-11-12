import frappe

# SAP properties each mapping reads, recorded by tracing the Mapper over 2,000 real Items
# (2026-11-12). Item rows carry ~31 KB of per-warehouse stock data the mapping never reads.
SELECT_FIELDS = {
	"SAP Item": (
		"ItemCode,ItemName,ItemType,ItemsGroupCode,Frozen,InventoryItem,PurchaseItem,SalesItem,"
		"InventoryUoMEntry,DefaultPurchasingUoMEntry,DefaultSalesUoMEntry,InventoryWeight,"
		"InventoryWeightUnit,ItemBarCodeCollection,ItemCountryOrg,LeadTime,ManageBatchNumbers,"
		"ManageSerialNumbers,Manufacturer,ProcurementMethod,AssetClass,AvgStdPrice,DefaultWarehouse,"
		"Mainsupplier,MinOrderQuantity,PurchaseItemsPerUnit,SalesItemsPerUnit,SupplierCatalogNo,User_Text"
	),
	"SAP Fixed Asset": "ItemCode,AssetClass,AssetStatus,CapitalizationDate,CreateDate",
}


def execute():
	frappe.reload_doc("sap", "doctype", "sap_doctype_mapping")
	for name, fields in SELECT_FIELDS.items():
		if frappe.db.exists("SAP Doctype Mapping", name):
			frappe.db.set_value(
				"SAP Doctype Mapping", name, "sap_select_fields", fields, update_modified=False
			)
