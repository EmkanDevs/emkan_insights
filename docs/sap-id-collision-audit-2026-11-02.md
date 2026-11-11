# SAP ID collision audit

Date: 2026-11-02. Site: sapinsight.emkan-dev.com.

The site has 66 enabled SAP mappings and 49 target DocTypes.
The scan completed all four companies for 51 mappings.
It confirmed an ID overlap in 53 mappings and an overlap between unprefixed companies in 21 mappings.
SAP rejected a renewed login for Saad Ali Al-Essa Group for Contracting after a stock endpoint timed out.
The 15 incomplete mappings use the IDs from the other three companies. Their overlap counts are lower bounds.

The scan read every ID from each available endpoint and each configured source array.
It applied each mapping filter and used the configured SAP ID fields.
An overlap means the same raw SAP ID occurs in at least two companies.
An exposed ID occurs in at least two companies whose mapping has Abbreviation Required off.
An exposed ID is a naming risk. It does not prove two records are different real-world entities.
A zero overlap does not prove future SAP IDs cannot collide.

## Confirmed different records with the same ID

| Target | SAP ID | Emkan | Petroleum Vision |
| --- | --- | --- | --- |
| Item | CO000001 | Roco A4 copy paper | ORS sachets |
| Supplier | V00001 | EZ oeder office supplies | Abdaa Al-Wassail Contracting Est. |
| Customer | C0001 | Saudi Veterinary Pharmacy Products Factory | Saudi Arabian Oil Company (Aramco) |
| Warehouse | 01 | EMKAN Warehouse | PV-Abqaiq |

## Recommended identity rules

- Keep the company prefix for documents and ledger rows. Their SAP IDs repeat across companies.
- Use company-specific names for warehouses, cost centers, bank accounts, employees, contacts, and projects. Their IDs repeat across companies.
- Keep an item, supplier, or customer shared only when a separate identity check confirms it is the same record.
- Store each company's SAP ID in a company-scoped reference to a shared item, supplier, or customer.
- Do not use the raw SAP ID alone as the key for a shared item, supplier, or customer.
- Preserve existing ERPNext root account names when the sync prefixes imported SAP accounts.
- Compare names and meaning before deciding whether groups, payment terms, price lists, asset categories, designations, and sales people are shared.
- Keep banks and units of measure shared when their code and meaning are the same across companies.

## Full mapping results

Counts and flags use this company order: Emkan / Petroleum Vision / Saad Ali Al-Essa / Samif.
Flag 1 means Abbreviation Required is on. Flag 0 means it is off.

| SAP mapping | ERPNext DocType | IDs by company | Flags | Shared IDs | Exposed IDs | Examples | Status |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| SAP Account | Account | 845 / 944 / 938 / 929 | 0/1/1/1 | 936 | 0 | 100000000000000, 1100000 | Complete |
| SAP Asset Category | Asset Category | 11 / 14 / 30 / 0 | 0/0/0/0 | 10 | 10 | 01, 05 | Complete |
| SAP Bank | Bank | 11 / 15 / 14 / 13 | 0/0/0/0 | 15 | 15 | AIB, ANB | Complete |
| SAP Bank Account | Bank Account | 1 / 15 / 23 / 7 | 0/0/0/0 | 15 | 15 | 1, 10 | Complete |
| SAP Blanket Order | Blanket Order | 9 / 56 / 475 / 21 | 1/1/1/1 | 8 | 0 | 100, 118 | Complete |
| SAP BOM | BOM | 0 / 0 / 0 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Contact | Contact | 37 / 430 / 1269 / 163 | 0/0/0/0 | 545 | 545 | 1, 10 | Complete |
| SAP Contract | Contract | 0 / 0 / 0 / 0 | 0/0/0/0 | 0 | 0 |  | Complete |
| SAP Cost Center | Cost Center | 86 / 1764 / 14899 / 350 | 0/0/0/0 | 50 | 50 | 01, 02 | Complete |
| SAP Customer | Customer | 5 / 51 / 580 / 56 | 0/0/0/0 | 56 | 56 | C0001, C0002 | Complete |
| SAP Customer Group | Customer Group | 1 / 5 / 16 / 3 | 0/0/0/0 | 6 | 6 | 100, 102 | Complete |
| SAP Delivery Note | Delivery Note | 1 / 0 / 0 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Department | Department | 12 / 11 / 29 / 11 | 1/1/1/1 | 12 | 0 | -2, 1 | Complete |
| SAP Designation | Designation | 27 / 92 / 124 / 0 | 0/0/0/0 | 91 | 91 | 1, 10 | Complete |
| SAP Employee | Employee | 41 / 1025 / 2851 / 332 | 0/0/0/0 | 189 | 189 | 437, 438 | Complete |
| SAP GL Journal Detail | GL Entry | 13022 / 43532 / 851099 / 6679 | 1/1/1/1 | 14,247 | 0 | 1000:0, 1000:1 | Complete |
| SAP Inventory Posting | Stock Reconciliation | 0 / 0 / 14 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Inventory Transfer Request | Material Request | 0 / 47 / 4319 / 0 | 1/1/1/1 | 12 | 0 | 151, 154 | Complete |
| SAP Item | Item | 550 / 27379 / 63127 / 22526 | 0/0/0/0 | 27,443 | 27,443 | CO000001, CO000002 | Complete |
| SAP Item Group | Item Group | 6 / 125 / 140 / 34 | 0/0/0/0 | 118 | 118 | 100, 101 | Complete |
| SAP Journal Entry | Journal Entry | 2563 / 14224 / 251549 / 1550 | 1/1/1/1 | 6,435 | 0 | 100, 1000 | Complete |
| SAP Lead | Lead | 1 / 0 / 0 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Lead Source | Lead Source | 1 / 0 / 0 / 0 | 0/0/0/0 | 0 | 0 |  | Complete |
| SAP Manufacturer | Manufacturer | 9 / 91 / 96 / 1 | 0/0/0/0 | 19 | 19 | -1, 1 | Complete |
| SAP Material Request | Material Request | 69 / 378 / 23761 / 580 | 1/1/1/1 | 434 | 0 | 1009, 1013 | Complete |
| SAP Mode of Payment | Mode of Payment | 0 / 1 / 2 / 0 | 0/0/0/0 | 1 | 1 | Check | Complete |
| SAP Opportunity | Opportunity | 1 / 0 / 0 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Payment Entry Incoming | Payment Entry | 117 / 153 / 3639 / 7 | 1/1/1/1 | 168 | 0 | 100, 101 | Complete |
| SAP Payment Entry Outgoing | Payment Entry | 440 / 1333 / 23808 / 352 | 1/1/1/1 | 1,436 | 0 | 100, 1000 | Complete |
| SAP Payment Incoming Allocation Detail | Payment Ledger Entry | 2 / 4 / 5851 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Payment Incoming Detail | Payment Ledger Entry | 2 / 105 / 2731 / 6 | 1/1/1/1 | 60 | 0 | 101, 122 | Complete |
| SAP Payment Outgoing Allocation Detail | Payment Ledger Entry | 21 / 830 / 22177 / 64 | 1/1/1/1 | 134 | 0 | 1002:0, 1083:0 | Complete |
| SAP Payment Outgoing Detail | Payment Ledger Entry | 62 / 873 / 9855 / 124 | 1/1/1/1 | 423 | 0 | 100, 1000 | Complete |
| SAP Payment Purchase Down Payment Detail | Payment Ledger Entry | 9 / 244 / 7 / 61 | 1/1/1/1 | 26 | 0 | 119, 135 | Complete |
| SAP Payment Purchase Invoice Detail | Payment Ledger Entry | 108 / 2181 / 32183 / 120 | 1/1/1/1 | 1,752 | 0 | 100, 1000 | Complete |
| SAP Payment Sales Invoice Detail | Payment Ledger Entry | 4 / 0 / 4949 / 0 | 1/1/1/1 | 2 | 0 | 55, 86 | Complete |
| SAP Payment Term | Payment Term | 15 / 7 / 18 / 14 | 0/0/0/0 | 15 | 15 | -1, 1 | Complete |
| SAP Price List | Price List | 11 / 10 / 10 / 10 | 0/0/0/0 | 10 | 10 | 1, 10 | Complete |
| SAP Project | Project | 4 / 58 / 303 / 118 | 0/0/0/0 | 58 | 58 | G&A, LFC-0001 | Complete |
| SAP Purchase Down Payment | Purchase Invoice | 9 / 244 / 7 / 61 | 1/1/1/1 | 26 | 0 | 119, 135 | Complete |
| SAP Purchase Invoice | Purchase Invoice | 108 / 2181 / 32183 / 120 | 1/1/1/1 | 1,752 | 0 | 100, 1000 | Complete |
| SAP Purchase Order | Purchase Order | 50 / 253 / 22229 / 451 | 1/1/1/1 | 321 | 0 | 1013, 1024 | Complete |
| SAP Purchase Receipt | Purchase Receipt | 2 / 662 / 23205 / 671 | 1/1/1/1 | 475 | 0 | 1001, 103 | Complete |
| SAP Quotation | Quotation | 1 / 0 / 0 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Sales Invoice | Sales Invoice | 4 / 0 / 4949 / 0 | 1/1/1/1 | 2 | 0 | 55, 86 | Complete |
| SAP Sales Order | Sales Order | 1 / 46 / 3630 / 0 | 1/1/1/1 | 25 | 0 | 103, 134 | Complete |
| SAP Sales Person | Sales Person | 4 / 5 / 22 / 4 | 0/0/0/0 | 5 | 5 | -1, 1 | Complete |
| SAP Sales Stage | Sales Stage | 1 / 3 / 3 / 0 | 0/0/0/0 | 3 | 3 | 1, 2 | Complete |
| SAP Sales Tax Code | Tax Category | 0 / 0 / 0 / 0 | 0/0/0/0 | 0 | 0 |  | Complete |
| SAP Stock Delivery Detail | Stock Ledger Entry | 1 / 0 / 0 / 0 | 1/1/1/1 | 0 | 0 |  | Complete |
| SAP Stock Entry Issue | Stock Entry | 1 / 5817 / NA / 43 | 1/1/1/1 | 23 | 0 | 102, 103 | SAE incomplete |
| SAP Stock Entry Receipt | Stock Entry | 1 / 261 / NA / 59 | 1/1/1/1 | 17 | 0 | 103, 106 | SAE incomplete |
| SAP Stock Entry Transfer | Stock Entry | 0 / 49 / NA / 339 | 1/1/1/1 | 34 | 0 | 109, 111 | SAE incomplete |
| SAP Stock Goods Issue Detail | Stock Ledger Entry | 2 / 23346 / NA / 6279 | 1/1/1/1 | 1,484 | 0 | 102:0, 102:1 | SAE incomplete |
| SAP Stock Goods Receipt Detail | Stock Ledger Entry | 1 / 2164 / NA / 6284 | 1/1/1/1 | 97 | 0 | 103:0, 106:0 | SAE incomplete |
| SAP Stock Purchase Receipt Detail | Stock Ledger Entry | 3 / 2237 / 49463 / 3575 | 1/1/1/1 | 742 | 0 | 1001:0, 1001:1 | Complete |
| SAP Stock Reconciliation | Stock Reconciliation | 0 / 0 / NA / 0 | 1/1/1/1 | 0 | 0 |  | SAE incomplete |
| SAP Stock Transfer Detail | Stock Ledger Entry | 0 / 1562 / NA / 1448 | 1/1/1/1 | 40 | 0 | 109:0, 109:1 | SAE incomplete |
| SAP Stock Transfer Receipt Detail | Stock Ledger Entry | 0 / 1562 / NA / 1448 | 1/1/1/1 | 40 | 0 | 109:0, 109:1 | SAE incomplete |
| SAP Supplier | Supplier | 74 / 633 / NA / 587 | 0/1/1/0 | 74 | 0 | V00001, V00002 | SAE incomplete |
| SAP Supplier Group | Supplier Group | 2 / 3 / NA / 11 | 1/1/1/1 | 3 | 0 | 101, 102 | SAE incomplete |
| SAP Supplier Quotation | Supplier Quotation | 72 / 358 / NA / 519 | 1/1/1/1 | 201 | 0 | 100, 1009 | SAE incomplete |
| SAP Territory | Territory | 5 / 1 / NA / 1 | 0/0/0/0 | 1 | 1 | -2 | SAE incomplete |
| SAP UOM | UOM | 18 / 59 / NA / 66 | 0/0/0/0 | 57 | 57 | ASSY, AU | SAE incomplete |
| SAP Warehouse | Warehouse | 2 / 2 / NA / 3 | 0/0/0/0 | 2 | 2 | 01, 02 | SAE incomplete |
| SAP Work Order | Work Order | 0 / 0 / NA / 0 | 1/1/1/1 | 0 | 0 |  | SAE incomplete |

## Incomplete SAP requests

- Saad Ali Al-Essa Group for Contracting: InventoryGenExits (SAP Stock Entry Issue, SAP Stock Goods Issue Detail). ConnectionError: HTTPSConnectionPool(host='sap.emkan-dev.com', port=443): Max retries exceeded with url: /b1s/v2/InventoryGenExits?%24select=DocEntry%2CDocumentLines (Caused by ReadTimeoutError("HTTPSConnectionPool(host='sap.emkan-dev.com', port=443): Read timed out.
- Saad Ali Al-Essa Group for Contracting: InventoryGenEntries (SAP Stock Entry Receipt, SAP Stock Goods Receipt Detail). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: StockTransfers (SAP Stock Entry Transfer, SAP Stock Transfer Detail, SAP Stock Transfer Receipt Detail). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: InventoryCountings (SAP Stock Reconciliation). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: BusinessPartners (SAP Supplier). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: BusinessPartnerGroups (SAP Supplier Group). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: PurchaseQuotations (SAP Supplier Quotation). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: Territories (SAP Territory). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: UnitOfMeasurements (SAP UOM). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: Warehouses (SAP Warehouse). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
- Saad Ali Al-Essa Group for Contracting: ProductionOrders (SAP Work Order). ValidationError: SAP Service Layer login failed (HTTP 500, SAP code 305): Switch company error: -1102
