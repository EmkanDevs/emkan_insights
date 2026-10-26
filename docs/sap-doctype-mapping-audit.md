# SAP DocType mapping audit

Audit date: 2026-10-23. Site: `sapinsight.emkan-dev.com`. SAP company: `EMKAN_LIVE_PBI`.

The audit used `sap_b1_client.py` to read up to three parent rows from each mapped SAP endpoint.
It sent each configured SAP filter. It did not save SAP record values or credentials.
It also checked every mapped SAP field against the live OData metadata. All 76 mappings passed that field check after corrections.
A successful response proves that the endpoint and filter worked during this audit.
A field seen in a sample is not proof that all records contain a value.
A field absent from a small sample needs more checks before a replacement is selected.

The live site has 76 mapping records. The source catalog has 52 entries.
The 24 extra live records include ledger review sources and other site mappings.
All 76 requests succeeded. Sixty-two responses had sample rows. Fourteen responses had no rows.
The report lists required ERP fields without a direct mapping. Site row rules or database defaults may supply them.
Twenty-two mappings have at least one such field. This audit did not test full ERP document creation.
An additional representative ERPNext validation pass checked the 22 mappings with required-field gaps. Six had no SAP rows, Sales Invoice samples were held as services, and Fixed Asset validation lacked a verified gross purchase amount. The other samples exposed the document and master-link issues noted below. In-memory validation does not prove that inserting or submitting every source row will succeed.
A read-only ERP link audit checked 603 parent Link fields across 56 mapped target DocTypes and 478 child Link fields. It found broken links in 13 parent fields and 6 child fields after the 414 Item Group links were repaired. The largest unresolved set is 13,020 Journal Entry Account child links. No broad repair was inferred from the counts alone.

The SAP sync used bulk database writes. These writes bypassed ERPNext Link validation and let source codes enter Link fields as invalid record names. The sync now resolves Account numbers within the document company and checks parent and child Link values before each bulk write. It rejects an unresolved batch and leaves its staging rows pending. Raw SAP responses with a sync hold now fail closed for every mapping, including Journal Entry. This check prevents new broken links. It does not repair links already written to ERPNext or make submitted accounting documents valid.

The bulk sync now also rejects submitted or canceled Journal Entries, Payments, Invoices, Receipts, Delivery Notes, Stock Entries, and Stock Reconciliations. ERPNext submission must create their GL or stock ledger entries. These SAP rows remain staged until a verified posting path exists.

The configuration job now records a document sync failure for one mapping and continues with later mappings. It returns `completed_with_errors` when any mapping fails after fetch. A SAP fetch failure still stops the job, so a Service Layer outage does not trigger repeated requests for every mapping.

## Changes

- Removed `Contact.department <- Department`. SAP contact rows had no `Department` field.
- Removed `Project.customer <- U_ClientCode`. SAP project rows had no `U_ClientCode` field.
- Set Item description to use `User_Text`, then `ItemName`. Sampled `User_Text` values were empty.
- Added verified Opportunity fields for the company, date, customer, amount, source, and sales stage.
- Corrected Inventory Counting and Posting detail dates, voucher fields, and line IDs using SAP OData metadata.
- Kept SAP service invoices in `External Sales Invoice` staging with their raw SAP response and a sync hold reason.
- Kept SAP service purchase invoices in `External Purchase Invoice` staging with their raw SAP response and a sync hold reason.
- Mapped invoice `ControlAccount` to the matching ERP Account for `credit_to` and `debit_to`.
- Summed SAP payment methods for `paid_amount`. A zero transfer amount no longer hides a cash payment.
- Held SAP payments in staging until their source and destination accounts and received amounts have verified mappings.
- Set the configured ERP naming series in the invoice and payment mappings.
- Set verified Receivable and Payable types on four invoice control accounts.
- Preserved SAP invoice posting dates during ERPNext validation and mapped Item UOM conversion factors.
- Synced one validated item Purchase Invoice into its existing ERP draft record.
- Resolved SAP Account parent codes to ERP Account names for future staging. Two conflicting source parent relationships stay held.
- Added Sales Order line UOM conversion and Stock Entry purpose mappings after live ERP validation.
- Held Purchase Orders with a line required date before the transaction date and Sales Orders whose ERP Items lack usable UOM data.
- Corrected Item Group links to use the ERP Item Group record name (SAP numeric code). Repaired 414 existing links after backup and approval.
- Corrected Customer, Supplier, Price List, and Asset Category mappings to use actual ERP link names. Existing affected links remain under review.
- Kept the 20 parent row limit on the fetch-only ledger review mappings. It still limits those sources.
- Fixed `sap_b1_client.py` pagination for the relative `@odata.nextLink` returned by SAP. A 20-row first page of Employee Positions now fetches all 27 rows.

## Field status

- **Value seen:** At least one sampled source row had a value.
- **Field seen, no value:** The response had the field, but sampled values were empty.
- **Collection empty in sample:** The response had the child collection, but it had no sampled rows.
- **No sample rows:** The endpoint returned zero rows. The mapping needs data for a row check.
- **Derived:** The mapping gets its value from a constant, the configuration, or a transform.

## SAP Account

- ERP DocType: `Account`. SAP endpoint: `ChartOfAccounts`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `account_name` | `Name / Code` | `join_fields` | Value seen |
| `account_number` | `FormatCode / Code` | `first_value` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `parent_account` | `FatherAccountKey` | `lookup Account.account_number` | Field seen, no value |
| `is_group` | `ActiveAccount` | `value_map` | Value seen |
| `disabled` | `FrozenFor` | `value_map` | Value seen |
| `freeze_account` | `FrozenFor` | `value_map` | Value seen |
| `account_currency` | `AcctCurrency` | `copy` | Field seen, no value |
| `account_type` | `Code / AccountType` | `conditional_value` | Value seen |
| `root_type` | `Code` | `conditional_value` | Value seen |
| `report_type` | `Code` | `conditional_value` | Value seen |

The live Account mapping now marks SAP control accounts `1131101` and `1131105` as Receivable, and `2131101` and `2131102` as Payable. The four matching ERP Accounts were corrected after checking their company, root type, and leaf status. Normal Account save validation failed because an existing parent account link is unresolved; the correction updated only `account_type`.
The Account parent mapping now resolves SAP codes to ERP Account names within the configured company. Of 854 existing ERP Accounts, 766 have parent links that still contain SAP codes instead of ERP names. A read-only review found 764 links that resolve to a group Account with the same root type. Two SAP Accounts have parent root conflicts and are held in staging. All new SAP Account fetches stay held until the hierarchy repair is approved, so a scheduled sync cannot apply the rejected bulk change. The existing Account hierarchy was not bulk changed. See [the repair review](sap-account-hierarchy-repair-review.md) and [the change manifest](sap-account-hierarchy-repair-review.json).

The `7,9,A` exclusion still blocks ERP Account sync and deletion. The 2026-10-26 fetch kept all 72 excluded SAP Accounts in raw staging with a sync hold. All ten missing Journal Entry account codes now have source records for accounting review.

## SAP Address (Business Partner)

- ERP DocType: `Address`. SAP endpoint: `BusinessPartners`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `AddressName`. Child source: `BPAddresses`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `address_title` | `AddressName` | `copy` | Value seen |
| `address_type` | `AddressType` | `value_map` | Value seen |
| `address_line1` | `Street` | `copy` | Field seen, no value |
| `address_line2` | `Block` | `copy` | Field seen, no value |
| `city` | `City` | `copy` | Value seen |
| `county` | `County` | `copy` | Value seen |
| `state` | `State` | `copy` | Value seen |
| `country` | `Country` | `copy` | Value seen |
| `pincode` | `ZipCode` | `copy` | Value seen |

## SAP Asset Category

- ERP DocType: `Asset Category`. SAP endpoint: `AssetClasses`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `asset_category_name` | `Description` | `copy` | Value seen |

## SAP BOM

- ERP DocType: `BOM`. SAP endpoint: `ProductTrees`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `TreeCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `item` | `TreeCode` | `copy` | No sample rows |
| `quantity` | `Quantity` | `copy` | No sample rows |
| `company` | `configuration context` | `context` | Derived |
| `currency` | `configuration context` | `context` | Derived |
| `conversion_rate` | `constant` | `constant` | Derived |
| `items.item_code` | `ItemCode` | `copy` | No sample rows |
| `items.qty` | `Quantity` | `copy` | No sample rows |
| `items.uom` | `InventoryUOM` | `copy` | No sample rows |
| `items.rate` | `Price` | `copy` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Bank

- ERP DocType: `Bank`. SAP endpoint: `Banks`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `BankCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `bank_name` | `BankName` | `copy` | Value seen |
| `swift_number` | `SwiftNo` | `copy` | Value seen |

## SAP Bank Account

- ERP DocType: `Bank Account`. SAP endpoint: `HouseBankAccounts`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `AbsoluteEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `account_name` | `AccountName / AccNo` | `first_value` | Value seen |
| `bank` | `BankCode` | `copy` | Value seen |
| `bank_account_no` | `AccNo` | `copy` | Value seen |
| `branch_code` | `Branch` | `copy` | Field seen, no value |
| `iban` | `IBAN` | `copy` | Field seen, no value |

## SAP Blanket Order

- ERP DocType: `Blanket Order`. SAP endpoint: `BlanketAgreements`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `AgreementNo`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `from_date` | `StartDate` | `sap_date` | Value seen |
| `to_date` | `EndDate` | `sap_date` | Value seen |
| `supplier` | `BPCode` | `copy` | Value seen |
| `blanket_order_type` | `Purchasing` | `constant` | Derived |
| `naming_series` | ERP Blanket Order series | `constant` | Derived |
| `items.item_code` | `ItemNo` | `copy` | Value seen |
| `items.qty` | `PlannedQuantity` | `copy` | Value seen |
| `items.rate` | `UnitPrice` | `copy` | Value seen |
| `docstatus` | `Status` | `value_map` | Value seen |

All nine live agreements reference Suppliers. The previous customer mapping put these supplier codes in an invalid Customer link. The corrected mapping passed in-memory ERPNext validation. Existing ERP Blanket Orders remain held for an approved direction repair. See [the review](sap-blanket-order-repair-review.md).

## SAP Contact

- ERP DocType: `Contact`. SAP endpoint: `BusinessPartners`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `InternalCode`. Child source: `ContactEmployees`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `first_name` | `FirstName / Name` | `first_value` | Value seen |
| `middle_name` | `MiddleName` | `copy` | Field seen, no value |
| `last_name` | `LastName` | `copy` | Value seen |
| `email_id` | `E_Mail` | `copy` | Value seen |
| `phone` | `Phone1` | `copy` | Value seen |
| `mobile_no` | `MobilePhone` | `copy` | Value seen |
| `designation` | `Position` | `copy` | Value seen |
| `unsubscribed` | `BlockSendingMarketingContent` | `value_map` | Value seen |

## SAP Contract

- ERP DocType: `Contract`. SAP endpoint: `ServiceContracts`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `ContractID`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `party_type` | `constant` | `constant` | Derived |
| `party_name` | `CustomerCode` | `copy` | No sample rows |
| `contract_terms` | `Remarks` | `copy` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Cost Center

- ERP DocType: `Cost Center`. SAP endpoint: `ProfitCenters`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `CenterCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `cost_center_name` | `CenterName` | `copy` | Value seen |
| `cost_center_number` | `CenterCode` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `is_group` | `CenterCode` | `sap_exists` | Value seen |
| `disabled` | `Active` | `value_map` | Value seen |

SAP `GroupCode` values such as `FN`, `HR`, and `IT` do not identify any SAP Profit Center or ERP Cost Center. The old `parent_cost_center` mapping produced five broken ERP parent links. It was removed. Source rows with group codes stay in raw staging until their ERP hierarchy is defined; existing parent links were not changed.

## SAP Customer

- ERP DocType: `Customer`. SAP endpoint: `BusinessPartners`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `CardCode`. Child source: `none`.
- Filter: `CardType eq 'cCustomer'`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `customer_name` | `CardName` | `copy` | Value seen |
| `customer_type` | `CompanyPrivate` | `value_map` | Value seen |
| `customer_group` | `GroupCode` | `lookup Customer Group.name` | Value seen |
| `tax_id` | `FederalTaxID` | `copy` | Value seen |
| `custom_vat_registration_number` | `VATRegistrationNumber` | `copy` | Field seen, no value |
| `website` | `Website` | `copy` | Field seen, no value |
| `customer_details` | `Notes` | `copy` | Field seen, no value |
| `default_currency` | `Currency` | `copy` | Value seen |
| `default_price_list` | `PriceListNum` | `lookup Price List.name` | Value seen |
| `default_commission_rate` | `CommissionPercent` | `copy` | Value seen |
| `disabled` | `Frozen` | `conditional_value` | Value seen |
| `is_frozen` | `Frozen` | `value_map` | Value seen |
| `mobile_no` | `Cellular` | `copy` | Field seen, no value |
| `email_id` | `EmailAddress` | `copy` | Field seen, no value |

The old mapping stored descriptive Customer Group and Price List labels as ERP Links. Five existing Customers have an unresolved link in each field. The new rules use the SAP numeric codes, and affected existing Customers remain held. See [the master link repair review](sap-master-link-repair-review.md).

## SAP Customer Group

- ERP DocType: `Customer Group`. SAP endpoint: `BusinessPartnerGroups`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `Type eq 'bbpgt_CustomerGroup'`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `customer_group_name` | `Name` | `copy` | Value seen |

## SAP Delivery Note

- ERP DocType: `Delivery Note`. SAP endpoint: `DeliveryNotes`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`, `selling_price_list`, `price_list_currency`, `plc_conversion_rate`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `customer` | `CardCode` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `customer_name` | `CardName` | `copy` | Value seen |

## SAP Department

- ERP DocType: `Department`. SAP endpoint: `Departments`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `department_name` | `Name` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |

## SAP Designation

- ERP DocType: `Designation`. SAP endpoint: `EmployeePosition`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `PositionID`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `designation_name` | `Name` | `copy` | Value seen |
| `description` | `Description` | `copy` | Value seen |

## SAP Designation (EmployeesInfo)

- ERP DocType: `Designation`. SAP endpoint: `EmployeesInfo`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `EmployeeID`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `designation_name` | `JobTitle` | `copy` | Value seen |

## SAP Employee

- ERP DocType: `Employee`. SAP endpoint: `EmployeesInfo`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `EmployeeID`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `first_name` | `FirstName` | `copy` | Value seen |
| `middle_name` | `MiddleName` | `copy` | Value seen |
| `last_name` | `LastName` | `copy` | Value seen |
| `employee_number` | `ExternalEmployeeNumber` | `copy` | Field seen, no value |
| `company` | `configuration context` | `context` | Derived |
| `status` | `Active` | `value_map` | Value seen |
| `gender` | `Gender` | `value_map` | Value seen |
| `marital_status` | `MartialStatus` | `value_map` | Value seen |
| `designation` | `JobTitle` | `lookup Designation.designation_name` | Value seen |
| `reports_to` | `Manager` | `copy` | Field seen, no value |
| `date_of_birth` | `DateOfBirth` | `sap_date` | Value seen |
| `date_of_joining` | `StartDate` | `sap_date` | Field seen, no value |
| `relieving_date` | `TerminationDate` | `sap_date` | Field seen, no value |
| `reason_for_leaving` | `TreminationReason` | `copy` | Field seen, no value |
| `cell_number` | `MobilePhone` | `copy` | Value seen |
| `personal_email` | `eMail` | `copy` | Value seen |
| `emergency_phone_number` | `HomePhone` | `copy` | Field seen, no value |
| `current_address` | `WorkStreet` | `copy` | Field seen, no value |
| `permanent_address` | `HomeStreet` | `copy` | Field seen, no value |
| `payroll_cost_center` | `CostCenterCode` | `copy` | Field seen, no value |
| `passport_number` | `PassportNumber` | `copy` | Value seen |
| `valid_upto` | `PassportExpirationDate` | `sap_date` | Field seen, no value |
| `date_of_issue` | `PassportIssueDate` | `sap_date` | Field seen, no value |
| `place_of_issue` | `PassportIssuer` | `copy` | Field seen, no value |
| `salary_currency` | `SalaryCurrency` | `copy` | Field seen, no value |
| `bank_name` | `BankCode` | `sap_lookup` | Field seen, no value |
| `bank_ac_no` | `BankAccount` | `copy` | Field seen, no value |
| `health_insurance_provider` | `HealthInsuranceName` | `copy` | Field seen, no value |
| `health_insurance_no` | `HealthInsuranceCode` | `copy` | Field seen, no value |

An unsaved in-memory Employee has no name, which makes ERPNext's self-manager check compare two empty values. Rechecking with the SAP Employee ID as the intended ERP name validated one employee without a manager and one with manager ID `66`; ERP Employee `66` exists.
The old `JobTitle` copy rule produced 39 broken Designation links. The new rule resolves a title to the ERP Designation record name. Twenty-nine existing links have one verified target; ten titles have no matching ERP Designation and stay held. See [the Designation review](sap-employee-designation-repair-review.md).

## SAP Fixed Asset

- ERP DocType: `Asset`. SAP endpoint: `Items`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `ItemCode`. Child source: `none`.
- Filter: `ItemType eq 'itFixedAssets'`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `location`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `asset_name` | `ItemName` | `copy` | Value seen |
| `purchase_date` | `CapitalizationDate` | `sap_date` | Value seen |

In-memory ERPNext validation of the filtered SAP sample failed because `gross_purchase_amount` is mandatory. The SAP Item response did not provide a verified acquisition amount or a usable ERP Asset location. This mapping remains disabled until those sources are identified and checked; a zero amount is not assumed.

## SAP GL Journal Detail

- ERP DocType: `GL Entry`. SAP endpoint: `JournalEntries`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `JdtNum+Line_ID`. Child source: `JournalEntryLines`.
- Filter: `OriginalJournal ne 'ttJournalEntry'`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `ReferenceDate` | `sap_date` | Value seen |
| `transaction_date` | `TaxDate` | `sap_date` | Value seen |
| `account` | `AccountCode` | `copy` | Value seen |
| `against` | `ContraAccount` | `copy` | Value seen |
| `cost_center` | `CostingCode` | `copy` | Field seen, no value |
| `debit` | `Debit` | `copy` | Value seen |
| `credit` | `Credit` | `copy` | Value seen |
| `debit_in_account_currency` | `FCDebit` | `sap_account_amount` | Value seen |
| `credit_in_account_currency` | `FCCredit` | `sap_account_amount` | Value seen |
| `account_currency` | `FCCurrency` | `sap_account_currency` | Field seen, no value |
| `project` | `ProjectCode` | `copy` | Value seen |
| `remarks` | `LineMemo` | `copy` | Value seen |
| `voucher_type` | `OriginalJournal` | `copy` | Value seen |
| `voucher_no` | `BaseReference` | `copy` | Value seen |
| `voucher_detail_no` | `Line_ID` | `copy` | Value seen |

## SAP Inventory Posting

- ERP DocType: `Stock Reconciliation`. SAP endpoint: `InventoryPostings`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocumentEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `purpose` | `constant` | `constant` | Derived |
| `posting_date` | `PostingDate` | `sap_date` | No sample rows |
| `items.item_code` | `ItemCode` | `copy` | No sample rows |
| `items.warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `items.qty` | `CountedQuantity` | `copy` | No sample rows |
| `items.valuation_rate` | `Price` | `copy` | No sample rows |
| `docstatus` | `constant` | `constant` | Derived |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Inventory Transfer Request

- ERP DocType: `Material Request`. SAP endpoint: `InventoryTransferRequests`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `material_request_type` | `constant` | `constant` | Derived |
| `transaction_date` | `DocDate` | `sap_date` | No sample rows |
| `schedule_date` | `DueDate` | `sap_date` | No sample rows |
| `items.item_code` | `ItemCode` | `copy` | No sample rows |
| `items.qty` | `Quantity` | `copy` | No sample rows |
| `items.warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `items.from_warehouse` | `FromWarehouseCode` | `copy` | No sample rows |
| `items.schedule_date` | `DueDate` | `parent_date` | No sample rows |
| `docstatus` | `DocumentStatus` | `value_map` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Item

- ERP DocType: `Item`. SAP endpoint: `Items`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `ItemCode`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `asset_category` | `AssetClass` | `lookup Asset Category.name` | Field seen, no value |
| `barcodes.barcode` | `Barcode` | `copy` | Collection empty in sample |
| `barcodes.uom` | `UoMEntry` | `sap_lookup` | Collection empty in sample |
| `country_of_origin` | `ItemCountryOrg` | `lookup` | Field seen, no value |
| `default_item_manufacturer` | `Manufacturer` | `sap_lookup` | Value seen |
| `default_material_request_type` | `ProcurementMethod` | `value_map` | Value seen |
| `description` | `User_Text / ItemName` | `first_value` | Value seen |
| `disabled` | `Frozen` | `value_map` | Value seen |
| `has_batch_no` | `ManageBatchNumbers` | `value_map` | Value seen |
| `has_serial_no` | `ManageSerialNumbers` | `value_map` | Value seen |
| `is_fixed_asset` | `ItemType` | `value_map` | Value seen |
| `is_purchase_item` | `PurchaseItem` | `value_map` | Value seen |
| `is_sales_item` | `SalesItem` | `value_map` | Value seen |
| `is_stock_item` | `InventoryItem` | `value_map` | Value seen |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `item_defaults.company` | `configuration context` | `context` | Derived |
| `item_defaults.default_supplier` | `Mainsupplier` | `copy` | Field seen, no value |
| `item_defaults.default_warehouse` | `DefaultWarehouse` | `copy` | Field seen, no value |
| `item_group` | `ItemsGroupCode` | `lookup Item Group.name` | Value seen |
| `item_name` | `ItemName` | `truncate` | Value seen |
| `lead_time_days` | `LeadTime` | `value_map` | Field seen, no value |
| `min_order_qty` | `MinOrderQuantity` | `copy` | Value seen |
| `purchase_uom` | `PurchaseUnit` | `copy` | Value seen |
| `sales_uom` | `SalesUnit` | `copy` | Value seen |
| `stock_uom` | `InventoryUOM` | `copy` | Value seen |
| `supplier_items.supplier` | `Mainsupplier` | `copy` | Field seen, no value |
| `supplier_items.supplier_part_no` | `SupplierCatalogNo` | `copy` | Field seen, no value |
| `uoms[1].conversion_factor` | `constant` | `constant` | Derived |
| `uoms[1].uom` | `InventoryUOM` | `copy` | Value seen |
| `uoms[2].conversion_factor` | `PurchaseItemsPerUnit` | `copy` | Value seen |
| `uoms[2].uom` | `PurchaseUnit` | `copy` | Value seen |
| `uoms[3].conversion_factor` | `SalesItemsPerUnit` | `copy` | Value seen |
| `uoms[3].uom` | `SalesUnit` | `copy` | Value seen |
| `valuation_rate` | `AvgStdPrice` | `copy` | Value seen |
| `weight_per_unit` | `InventoryWeight` | `copy` | Value seen |
| `weight_uom` | `InventoryWeightUnit` | `value_map` | Field seen, no value |
| `custom_sap_full_item_name` | `ItemName` | `copy` | Value seen |

SAP Item Groups on this site use numeric SAP codes as ERP record names (for example `104`), while the old Item mapping stored the descriptive SAP group name (for example `CONSUMABLES`). After approval and a database backup, all 414 broken `item_group` links were repaired. The corrected mapping is used for future records. See [the Item Group repair review](sap-item-group-repair-review.md). Separate Asset Category and UOM links still require review.

The old Asset Category mapping stored a description where ERP uses an SAP code as its record name. The new rule uses the code. The 114 existing broken Asset Category links remain held. Three Items use UOM name `exce`, which has no ERP UOM record. SAP `InventoryUoMEntry` `5` points to a UOM record with code `KG` and name `exce`. A physical-unit decision is needed before changing their stock, purchase, and sales UOM links.
The old Item supplier child rules stored SAP Business Partner names instead of ERP Supplier codes. Three existing Item Supplier links and three Item default Supplier links are broken. The new rules use `Mainsupplier` directly. A SAP Warehouse code, when present, is now copied directly into the Item default Warehouse link. See [the child-link repair review](sap-item-child-link-repair-review.md).

## SAP Item Group

- ERP DocType: `Item Group`. SAP endpoint: `ItemGroups`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Number`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `item_group_name` | `GroupName` | `copy` | Value seen |
| `is_group` | `U_Type` | `value_map` | Value seen |
| `item_group_defaults.company` | `configuration context` | `context` | Derived |
| `item_group_defaults.expense_account` | `ExpensesAccount` | `lookup Account.account_number and company` | Field seen, no value |
| `item_group_defaults.income_account` | `RevenuesAccount` | `lookup Account.account_number and company` | Field seen, no value |

SAP `ComponentWarehouse` is an enum value such as `bomcw_BOM`, not an ERP Warehouse code. The old mapping placed it in five Item Group default Warehouse links. The field was removed from future mapping, and existing affected groups stay held. Group default Account codes now resolve to ERP Account names when present. See [the child-link repair review](sap-item-child-link-repair-review.md).

## SAP Journal Entry

- ERP DocType: `Journal Entry`. SAP endpoint: `JournalEntries`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `JdtNum`. Child source: `none`.
- Filter: `OriginalJournal eq 'ttJournalEntry'`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `ReferenceDate` | `sap_date` | Value seen |
| `user_remark` | `Memo` | `copy` | Value seen |
| `accounts.account` | `AccountCode` | `lookup Account.account_number and company` | Value seen |
| `accounts.debit_in_account_currency` | `FCDebit` | `sap_account_amount` | Value seen |
| `accounts.credit_in_account_currency` | `FCCredit` | `sap_account_amount` | Value seen |
| `accounts.user_remark` | `LineMemo` | `copy` | Value seen |
| `accounts.cost_center` | `CostingCode` | `copy` | Field seen, no value |
| `accounts.project` | `ProjectCode` | `copy` | Value seen |
| `accounts.account_currency` | `FCCurrency` | `sap_account_currency` | Field seen, no value |
| `accounts.debit` | `Debit` | `copy` | Value seen |
| `accounts.credit` | `Credit` | `copy` | Value seen |
| `docstatus` | `constant` | `constant` | Derived |
| `due_date` | `DueDate` | `sap_date` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |

All 2,562 existing ERP Journal Entries are marked submitted, but their 13,020 Account child links contain SAP account codes rather than ERP Account names. Of those lines, 12,937 resolve by `account_number` and company; 83 do not. The ERP site has zero GL Entries. Updating submitted Journal Entry rows alone would not create the missing ledger entries. Future SAP Journal Entries stay in raw staging while the Account hierarchy and finance posting process are reviewed. The new mapping resolves known account codes but does not release this hold.

A later read-only check found that the 83 unresolved lines use ten account numbers excluded by the SAP Account mapping's `7,9,A` prefix rule. It also found 12,983 lines with zero local debit and credit but nonzero account currency amounts. See [the Journal Entry account review](sap-journal-account-review.md).

## SAP Lead

- ERP DocType: `Lead`. SAP endpoint: `BusinessPartners`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `CardCode`. Child source: `none`.
- Filter: `CardType eq 'cLid'`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `lead_name` | `CardName` | `copy` | Value seen |
| `first_name` | `ContactPerson / CardName` | `first_value` | Value seen |
| `company_name` | `CardName` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `email_id` | `EmailAddress` | `copy` | Field seen, no value |
| `phone` | `Phone1` | `copy` | Field seen, no value |
| `mobile_no` | `Cellular` | `copy` | Field seen, no value |
| `fax` | `Fax` | `copy` | Field seen, no value |
| `website` | `Website` | `copy` | Field seen, no value |
| `city` | `City` | `copy` | Field seen, no value |
| `state` | `BillToState` | `copy` | Field seen, no value |
| `country` | `Country` | `lookup` | Value seen |
| `disabled` | `Frozen` | `conditional_value` | Value seen |
| `unsubscribed` | `BlockSendingMarketingContent` | `value_map` | Value seen |

## SAP Lead Source

- ERP DocType: `Lead Source`. SAP endpoint: `SalesOpportunitySourcesSetup`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `SequenceNo`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `source_name` | `Description` | `copy` | Value seen |

## SAP Location

- ERP DocType: `Location`. SAP endpoint: `Warehouses`.
- Live state: disabled. Sample: 2 parent rows. Response: `ok`.
- SAP ID: `WarehouseCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `location_name` | `WarehouseName` | `copy` | Value seen |

## SAP Manufacturer

- ERP DocType: `Manufacturer`. SAP endpoint: `Manufacturers`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `short_name` | `ManufacturerName` | `copy` | Value seen |
| `full_name` | `ManufacturerName` | `copy` | Value seen |

## SAP Material Request

- ERP DocType: `Material Request`. SAP endpoint: `PurchaseRequests`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `transaction_date` | `DocDate` | `sap_date` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.schedule_date` | `ShipDate` | `sap_date` | Field seen, no value |
| `material_request_type` | `constant` | `constant` | Derived |
| `schedule_date` | `DocDueDate` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |

## SAP Mode of Payment

- ERP DocType: `Mode of Payment`. SAP endpoint: `WizardPaymentMethods`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `PaymentMethodCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `custom_zatca_payment_means_code`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `mode_of_payment` | `Description / PaymentMethodCode` | `first_value` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Opportunity

- ERP DocType: `Opportunity`. SAP endpoint: `SalesOpportunities`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `SequentialNo`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `title` | `OpportunityName` | `copy` | Value seen |
| `party_name` | `CardCode` | `copy` | Value seen |
| `opportunity_from` | `constant` | `constant` | Derived |
| `company` | `configuration context` | `context` | Derived |
| `transaction_date` | `StartDate` | `sap_date` | Value seen |
| `customer_name` | `CustomerName` | `copy` | Value seen |
| `expected_closing` | `PredictedClosingDate` | `sap_date` | Value seen |
| `probability` | `ClosingPercentage` | `copy` | Value seen |
| `opportunity_amount` | `TotalAmountLocal` | `copy` | Value seen |
| `source` | `Source` | `sap_lookup` | Value seen |
| `sales_stage` | `CurrentStageNo` | `sap_lookup` | Value seen |

The audit confirmed that `Source` and `CurrentStageNo` resolve to SAP setup records. The mapping leaves ERP status at its default until all SAP status values are checked.

## SAP Payment Entry Incoming

- ERP DocType: `Payment Entry`. SAP endpoint: `IncomingPayments`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none. Payments remain held while allocations and account types are checked.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `naming_series` | `ACC-PAY-.YYYY.-` | `constant` | Derived |
| `payment_type` | `constant` | `constant` | Derived |
| `party_type` | `DocType` | `conditional_value` | Value seen |
| `party` | `CardCode` | `copy` | Value seen |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `paid_amount` | `TransferSum + CashSum + CheckSum + CreditSum` | `sum_fields` | Value seen |
| `paid_from` | `ControlAccount` | `lookup Account.account_number` | Value seen |
| `paid_to` | `TransferAccount / CashAccount` | `payment_method_account` | Value seen |
| `paid_from_account_currency` | `ControlAccount` | `lookup Account.account_currency` | Value seen |
| `paid_to_account_currency` | `TransferAccount / CashAccount` | `payment_method_account` | Value seen |
| `received_amount` | `TransferSum + CashSum + CheckSum + CreditSum` | `sum_fields` | Value seen |
| `source_exchange_rate` | SAP and ERP account currencies | `local_payment_value` | Derived |
| `target_exchange_rate` | SAP and ERP account currencies | `local_payment_value` | Derived |
| `base_paid_amount` | SAP payment methods | `local_payment_value` | Value seen |
| `base_received_amount` | SAP payment methods | `local_payment_value` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `party_name` | `CardName` | `copy` | Value seen |
| `reference_no` | `TransferReference` | `copy` | Field seen, no value |
| `reference_date` | `TransferDate` | `sap_date` | Value seen |
| `remarks` | `Remarks` | `copy` | Field seen, no value |

All 117 incoming payments are staged with the full SAP response and a sync hold reason. SAP marks 114 as `rAccount`, two as `rCustomer`, and one as `rSupplier`. The party type rule maps the two customer payments to Customer and the supplier payment to Supplier. It rejects all 114 account payments. All three party payments passed the account, currency, and amount mapper check. Account payments need a separate accounting mapping. Existing ERP Payment Entries were not changed.

## SAP Payment Entry Outgoing

- ERP DocType: `Payment Entry`. SAP endpoint: `VendorPayments`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none. Payments remain held while allocations and account types are checked.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `naming_series` | `ACC-PAY-.YYYY.-` | `constant` | Derived |
| `payment_type` | `constant` | `constant` | Derived |
| `party_type` | `DocType` | `conditional_value` | Value seen |
| `party` | `CardCode` | `copy` | Value seen |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `paid_amount` | `TransferSum + CashSum + CheckSum + CreditSum` | `sum_fields` | Value seen |
| `paid_from` | `TransferAccount / CashAccount` | `payment_method_account` | Value seen |
| `paid_to` | `ControlAccount` | `lookup Account.account_number` | Value seen |
| `paid_from_account_currency` | `TransferAccount / CashAccount` | `payment_method_account` | Value seen |
| `paid_to_account_currency` | `ControlAccount` | `lookup Account.account_currency` | Value seen |
| `received_amount` | `TransferSum + CashSum + CheckSum + CreditSum` | `sum_fields` | Value seen |
| `source_exchange_rate` | SAP and ERP account currencies | `local_payment_value` | Derived |
| `target_exchange_rate` | SAP and ERP account currencies | `local_payment_value` | Derived |
| `base_paid_amount` | SAP payment methods | `local_payment_value` | Value seen |
| `base_received_amount` | SAP payment methods | `local_payment_value` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `party_name` | `CardName` | `copy` | Value seen |
| `reference_no` | `TransferReference` | `copy` | Value seen |
| `reference_date` | `TransferDate` | `sap_date` | Value seen |
| `remarks` | `Remarks` | `copy` | Field seen, no value |

All 440 outgoing payments are staged with the full SAP response and a sync hold reason. SAP marks 378 as `rAccount` and 62 as `rSupplier`. The party type rule maps the 62 supplier payments to Supplier and rejects all 378 account payments. All 62 supplier payments passed the account, currency, and amount mapper check. Their ERP account types are still unset. Across the 65 party payments, 23 SAP invoice allocations exist: 16 purchase invoices, four purchase down payments, two sales invoices, and one journal entry. The current Payment Entry mapping does not preserve those allocations. Account payments need a separate accounting mapping. Seven outgoing account payments have line totals that differ from the payment method total, and two line account codes do not resolve in ERP. Existing ERP Payment Entries were not changed. Across both payment types, 216 existing ERP Payment Entries have zero `paid_amount` from the old first-value rule; all 557 have empty account and `received_amount` fields. These historical records need reconciliation before they can be treated as valid accounting data.

## SAP Payment Incoming Allocation Detail

- ERP DocType: `Payment Ledger Entry`. SAP endpoint: `IncomingPayments`.
- Live state: enabled. Sample: 2 parent rows. Response: `ok`.
- SAP ID: `__parent_DocEntry+__line_index`. Child source: `PaymentInvoices`.
- Filter: `DocType eq 'rCustomer'`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `__parent_DocDate` | `sap_date` | Value seen |
| `account` | `__parent_ControlAccount` | `copy` | Value seen |
| `party` | `__parent_CardCode` | `copy` | Value seen |
| `account_currency` | `__parent_DocCurrency` | `copy` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |
| `voucher_no` | `__parent_DocNum` | `copy` | Value seen |
| `project` | `__parent_ProjectCode` | `copy` | Field seen, no value |
| `remarks` | `Remarks` | `copy` | Field seen, no value |
| `account_type` | `constant` | `constant` | Derived |
| `party_type` | `constant` | `constant` | Derived |
| `against_voucher_type` | `InvoiceType` | `copy` | Value seen |
| `against_voucher_no` | `DocEntry` | `copy` | Value seen |
| `voucher_detail_no` | `__line_index` | `copy` | Derived |
| `amount` | `SumApplied` | `copy` | Value seen |

## SAP Payment Incoming Detail

- ERP DocType: `Payment Ledger Entry`. SAP endpoint: `IncomingPayments`.
- Live state: enabled. Sample: 2 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `DocType eq 'rCustomer'`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `account` | `ControlAccount` | `copy` | Value seen |
| `party` | `CardCode` | `copy` | Value seen |
| `account_currency` | `DocCurrency` | `copy` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `project` | `ProjectCode` | `copy` | Field seen, no value |
| `remarks` | `Remarks` | `copy` | Field seen, no value |
| `account_type` | `constant` | `constant` | Derived |
| `party_type` | `constant` | `constant` | Derived |

## SAP Payment Outgoing Allocation Detail

- ERP DocType: `Payment Ledger Entry`. SAP endpoint: `VendorPayments`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `__parent_DocEntry+__line_index`. Child source: `PaymentInvoices`.
- Filter: `DocType eq 'rSupplier'`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `__parent_DocDate` | `sap_date` | Value seen |
| `account` | `__parent_ControlAccount` | `copy` | Value seen |
| `party` | `__parent_CardCode` | `copy` | Value seen |
| `account_currency` | `__parent_DocCurrency` | `copy` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |
| `voucher_no` | `__parent_DocNum` | `copy` | Value seen |
| `project` | `__parent_ProjectCode` | `copy` | Field seen, no value |
| `remarks` | `Remarks` | `copy` | Field seen, no value |
| `account_type` | `constant` | `constant` | Derived |
| `party_type` | `constant` | `constant` | Derived |
| `against_voucher_type` | `InvoiceType` | `copy` | Value seen |
| `against_voucher_no` | `DocEntry` | `copy` | Value seen |
| `voucher_detail_no` | `__line_index` | `copy` | Derived |
| `amount` | `SumApplied` | `copy` | Value seen |

## SAP Payment Outgoing Detail

- ERP DocType: `Payment Ledger Entry`. SAP endpoint: `VendorPayments`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `DocType eq 'rSupplier'`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `account` | `ControlAccount` | `copy` | Value seen |
| `party` | `CardCode` | `copy` | Value seen |
| `account_currency` | `DocCurrency` | `copy` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `project` | `ProjectCode` | `copy` | Field seen, no value |
| `remarks` | `Remarks` | `copy` | Field seen, no value |
| `account_type` | `constant` | `constant` | Derived |
| `party_type` | `constant` | `constant` | Derived |

## SAP Payment Purchase Invoice Detail

- ERP DocType: `Payment Ledger Entry`. SAP endpoint: `PurchaseInvoices`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `account` | `ControlAccount` | `copy` | Value seen |
| `party` | `CardCode` | `copy` | Value seen |
| `account_currency` | `DocCurrency` | `copy` | Value seen |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `account_type` | `constant` | `constant` | Derived |
| `party_type` | `constant` | `constant` | Derived |
| `voucher_type` | `constant` | `constant` | Derived |

## SAP Payment Sales Invoice Detail

- ERP DocType: `Payment Ledger Entry`. SAP endpoint: `Invoices`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `account` | `ControlAccount` | `copy` | Value seen |
| `party` | `CardCode` | `copy` | Value seen |
| `account_currency` | `DocCurrency` | `copy` | Value seen |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `account_type` | `constant` | `constant` | Derived |
| `party_type` | `constant` | `constant` | Derived |
| `voucher_type` | `constant` | `constant` | Derived |

## SAP Payment Term

- ERP DocType: `Payment Term`. SAP endpoint: `PaymentTermsTypes`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `GroupNumber`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `payment_term_name` | `PaymentTermsGroupName` | `copy` | Value seen |
| `due_date_based_on` | `StartFrom` | `conditional_value` | Value seen |
| `credit_days` | `NumberOfAdditionalDays` | `copy` | Value seen |
| `credit_months` | `NumberOfAdditionalMonths` | `copy` | Value seen |

## SAP Price List

- ERP DocType: `Price List`. SAP endpoint: `PriceLists`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `PriceListNo`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `price_list_name` | `PriceListName` | `copy` | Value seen |
| `currency` | `DefaultPrimeCurrency` | `copy` | Value seen |
| `buying` | `PriceListNo` | `sap_exists` | Value seen |
| `selling` | `PriceListNo` | `sap_exists` | Value seen |
| `enabled` | `Active` | `value_map` | Value seen |

## SAP Production Plan

- ERP DocType: `Production Plan`. SAP endpoint: `ProductionOrders`.
- Live state: disabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `AbsoluteEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `PostingDate` | `sap_date` | No sample rows |
| `item_code` | `ItemNo` | `copy` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Project

- ERP DocType: `Project`. SAP endpoint: `Projects`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `project_name` | `Name` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `expected_start_date` | `ValidFrom` | `sap_date` | Value seen |
| `expected_end_date` | `ValidTo` | `sap_date` | Field seen, no value |
| `is_active` | `Active` | `value_map` | Value seen |

## SAP Project Type

- ERP DocType: `Project Type`. SAP endpoint: `Projects`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `project_type` | `Name` | `copy` | Value seen |

## SAP Purchase Invoice

- ERP DocType: `Purchase Invoice`. SAP endpoint: `PurchaseInvoices`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `naming_series` | `ACC-PINV-.YYYY.-` | `constant` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `set_posting_time` | `1` | `constant` | Derived |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.conversion_factor` | ERP Item UOM conversion | `item_conversion_factor` | Derived |
| `supplier` | `CardCode` | `copy` | Value seen |
| `supplier_name` | `CardName` | `copy` | Value seen |
| `credit_to` | `ControlAccount` | `lookup Account.account_number` | Value seen |
| `due_date` | `DocDueDate` | `sap_date` | Value seen |
| `bill_no` | `NumAtCard` | `copy` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `remarks` | `Comments` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

The live SAP source has 108 purchase invoices: 96 service invoices and 12 item invoices. The 96 service invoices remain held until they have an ERP item mapping. Eleven item invoices remain held because their SAP Items have no stock UOM or are not marked as purchase items. One item invoice passed ERPNext validation and synced into its existing draft Purchase Invoice. Its posting date, due date, payable account, two Item rows, and conversion factors match the staged mapped data. The other 107 existing ERP Purchase Invoices still need historical reconciliation.

## SAP Purchase Order

- ERP DocType: `Purchase Order`. SAP endpoint: `PurchaseOrders`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `transaction_date` | `DocDate` | `sap_date` | Value seen |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.schedule_date` | `ShipDate` | `sap_date` | Value seen |
| `supplier` | `CardCode` | `copy` | Value seen |
| `supplier_name` | `CardName` | `copy` | Value seen |
| `schedule_date` | `DocDueDate` | `copy` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

The sampled SAP line `ShipDate` preceded its parent `DocDate`. ERPNext rejects this required date. Such records remain in raw staging until the source date is corrected or a reviewed business rule is supplied; the importer does not change the date.

## SAP Purchase Receipt

- ERP DocType: `Purchase Receipt`. SAP endpoint: `PurchaseDeliveryNotes`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`, `base_net_total`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.schedule_date` | `ShipDate` | `sap_date` | Value seen |
| `supplier` | `CardCode` | `copy` | Value seen |
| `supplier_name` | `CardName` | `copy` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `remarks` | `Comments` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

The sampled receipt's Item Group link was repaired. Its Supplier still has an invalid Price List link, so the receipt remains in staging.

## SAP Purchase Taxes and Charges Template

- ERP DocType: `Purchase Taxes and Charges Template`. SAP endpoint: `VatGroups`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `Category eq 'bovcInputTax'`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `title` | `Name` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `disabled` | `Inactive` | `sap_bool` | Value seen |

## SAP Quotation

- ERP DocType: `Quotation`. SAP endpoint: `Quotations`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`, `selling_price_list`, `price_list_currency`, `plc_conversion_rate`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `transaction_date` | `DocDate` | `sap_date` | Value seen |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `quotation_to` | `constant` | `constant` | Derived |
| `party_name` | `CardCode` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `customer_name` | `CardName` | `copy` | Value seen |

## SAP Sales Invoice

- ERP DocType: `Sales Invoice`. SAP endpoint: `Invoices`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `selling_price_list`, `price_list_currency`, `plc_conversion_rate`, `base_net_total`, `base_grand_total`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `naming_series` | `ACC-SINV-.YYYY.-` | `constant` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `set_posting_time` | `1` | `constant` | Derived |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Field seen, no value |
| `items.item_name` | `ItemDescription` | `copy` | Field seen, no value |
| `items.description` | `ItemDescription` | `copy` | Field seen, no value |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Field seen, no value |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Field seen, no value |
| `items.conversion_factor` | ERP Item UOM conversion | `item_conversion_factor` | No item lines to check |
| `customer` | `CardCode` | `copy` | Value seen |
| `customer_name` | `CardName` | `copy` | Value seen |
| `debit_to` | `ControlAccount` | `lookup Account.account_number` | Value seen |
| `due_date` | `DocDueDate` | `sap_date` | Value seen |
| `po_no` | `NumAtCard` | `copy` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `remarks` | `Comments` | `copy` | Field seen, no value |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

The three sampled invoices had SAP type `dDocument_Service`. Their lines had no `ItemCode`, `ItemDescription`, `UoMCode`, or `WarehouseCode` values. The Item line mapping needs a separate service line design.
The live SAP source has four service invoices. All four are staged with their raw SAP response and a sync hold reason.
The ERP sync query excludes held rows. The four existing ERP Sales Invoices were not changed by this fetch.
The mapped SAP control accounts now have ERP Receivable account types. The existing ERP Sales Invoices still have empty `debit_to` fields and need historical reconciliation.

## SAP Sales Order

- ERP DocType: `Sales Order`. SAP endpoint: `Orders`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`, `selling_price_list`, `price_list_currency`, `plc_conversion_rate`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `transaction_date` | `DocDate` | `sap_date` | Value seen |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `items.conversion_factor` | ERP Item UOM table | `item_conversion_factor` | Derived |
| `items.delivery_date` | `ShipDate` | `sap_date` | Value seen |
| `customer` | `CardCode` | `copy` | Value seen |
| `customer_name` | `CardName` | `copy` | Value seen |
| `delivery_date` | `DocDueDate` | `copy` | Value seen |
| `po_no` | `NumAtCard` | `copy` | Field seen, no value |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

ERPNext requires a line conversion factor. The mapping resolves it from the ERP Item stock UOM or UOM conversion row. The sampled Item has no stock UOM, so this Sales Order remains in raw staging until the Item is completed.

## SAP Sales Person

- ERP DocType: `Sales Person`. SAP endpoint: `SalesPersons`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `SalesEmployeeCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `sales_person_name` | `SalesEmployeeName` | `copy` | Value seen |
| `enabled` | `Active` | `conditional_value` | Value seen |
| `employee` | `EmployeeID` | `copy` | Field seen, no value |
| `commission_rate` | `CommissionForSalesEmployee` | `copy` | Value seen |

## SAP Sales Stage

- ERP DocType: `Sales Stage`. SAP endpoint: `SalesStages`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `SequenceNo`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `stage_name` | `Name` | `copy` | Value seen |

## SAP Sales Tax Code

- ERP DocType: `Tax Category`. SAP endpoint: `SalesTaxCodes`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `title` | `Code` | `copy` | No sample rows |
| `disabled` | `Inactive` | `sap_bool` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Sales Taxes and Charges Template

- ERP DocType: `Sales Taxes and Charges Template`. SAP endpoint: `VatGroups`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `Category eq 'bovcOutputTax'`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `title` | `Name` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `disabled` | `Inactive` | `sap_bool` | Value seen |

## SAP Stock Counting Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `InventoryCountings`.
- Live state: disabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocumentEntry+LineNumber`. Child source: `InventoryCountingLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `CountDate` | `sap_date` | No sample rows |
| `item_code` | `ItemCode` | `copy` | No sample rows |
| `warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `voucher_no` | `DocumentNumber` | `copy` | No sample rows |
| `voucher_detail_no` | `LineNumber` | `copy` | No sample rows |
| `project` | `ProjectCode` | `copy` | No sample rows |
| `stock_uom` | `UoMCode` | `copy` | No sample rows |
| `voucher_type` | `constant` | `constant` | Derived |

No parent rows were available in the sampled response. SAP OData metadata confirms the listed source fields and child line ID.

## SAP Stock Delivery Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `DeliveryNotes`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry+LineNum`. Child source: `DocumentLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `warehouse` | `WarehouseCode` | `copy` | Value seen |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `voucher_detail_no` | `LineNum` | `copy` | Value seen |
| `project` | `ProjectCode` | `copy` | Field seen, no value |
| `stock_uom` | `UoMCode` | `copy` | Value seen |
| `actual_qty` | `Quantity` | `multiply` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |

## SAP Stock Entry Issue

- ERP DocType: `Stock Entry`. SAP endpoint: `InventoryGenExits`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `stock_entry_type` | `constant` | `constant` | Derived |
| `purpose` | `Material Issue` | `constant` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.s_warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

The sampled issue passed ERPNext validation after its Item Group links were repaired.

## SAP Stock Entry Receipt

- ERP DocType: `Stock Entry`. SAP endpoint: `InventoryGenEntries`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `stock_entry_type` | `constant` | `constant` | Derived |
| `purpose` | `Material Receipt` | `constant` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.t_warehouse` | `WarehouseCode` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |

The sampled receipt passed ERPNext validation after its Item Group link was repaired.

## SAP Stock Entry Transfer

- ERP DocType: `Stock Entry`. SAP endpoint: `StockTransfers`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `stock_entry_type` | `constant` | `constant` | Derived |
| `posting_date` | `DocDate` | `sap_date` | No sample rows |
| `items.item_code` | `ItemCode` | `copy` | No sample rows |
| `items.qty` | `Quantity` | `copy` | No sample rows |
| `items.s_warehouse` | `FromWarehouseCode` | `copy` | No sample rows |
| `items.t_warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `docstatus` | `DocumentStatus` | `value_map` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Stock Goods Issue Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `InventoryGenExits`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry+LineNum`. Child source: `DocumentLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `warehouse` | `WarehouseCode` | `copy` | Value seen |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `voucher_detail_no` | `LineNum` | `copy` | Value seen |
| `project` | `ProjectCode` | `copy` | Value seen |
| `stock_uom` | `UoMCode` | `copy` | Value seen |
| `actual_qty` | `Quantity` | `multiply` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |

## SAP Stock Goods Receipt Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `InventoryGenEntries`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry+LineNum`. Child source: `DocumentLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `warehouse` | `WarehouseCode` | `copy` | Value seen |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `voucher_detail_no` | `LineNum` | `copy` | Value seen |
| `project` | `ProjectCode` | `copy` | Field seen, no value |
| `stock_uom` | `UoMCode` | `copy` | Value seen |
| `actual_qty` | `Quantity` | `multiply` | Value seen |
| `incoming_rate` | `StockInmPrice` | `copy` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |

## SAP Stock Item Warehouse Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `Items`.
- Live state: disabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `ItemCode`. Child source: `ItemWarehouseInfoCollection`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `warehouse` | `WarehouseCode` | `copy` | Value seen |
| `qty_after_transaction` | `InStock` | `copy` | Value seen |
| `valuation_rate` | `StandardAveragePrice` | `copy` | Value seen |

## SAP Stock Posting Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `InventoryPostings`.
- Live state: disabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocumentEntry+LineNumber`. Child source: `InventoryPostingLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `PostingDate` | `sap_date` | No sample rows |
| `item_code` | `ItemCode` | `copy` | No sample rows |
| `warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `voucher_no` | `DocumentNumber` | `copy` | No sample rows |
| `voucher_detail_no` | `LineNumber` | `copy` | No sample rows |
| `project` | `ProjectCode` | `copy` | No sample rows |
| `stock_uom` | `UoMCode` | `copy` | No sample rows |
| `voucher_type` | `constant` | `constant` | Derived |

No parent rows were available in the sampled response. SAP OData metadata confirms the listed source fields and child line ID.

## SAP Stock Purchase Receipt Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `PurchaseDeliveryNotes`.
- Live state: enabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `DocEntry+LineNum`. Child source: `DocumentLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | Value seen |
| `item_code` | `ItemCode` | `copy` | Value seen |
| `warehouse` | `WarehouseCode` | `copy` | Value seen |
| `voucher_no` | `DocNum` | `copy` | Value seen |
| `voucher_detail_no` | `LineNum` | `copy` | Value seen |
| `project` | `ProjectCode` | `copy` | Value seen |
| `stock_uom` | `UoMCode` | `copy` | Value seen |
| `actual_qty` | `Quantity` | `multiply` | Value seen |
| `incoming_rate` | `StockInmPrice` | `copy` | Value seen |
| `voucher_type` | `constant` | `constant` | Derived |

## SAP Stock Reconciliation

- ERP DocType: `Stock Reconciliation`. SAP endpoint: `InventoryCountings`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocumentEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `CountDate` | `sap_date` | No sample rows |
| `purpose` | `constant` | `constant` | Derived |
| `items.item_code` | `ItemCode` | `copy` | No sample rows |
| `items.warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `items.qty` | `CountedQuantity` | `copy` | No sample rows |
| `docstatus` | `DocumentStatus` | `value_map` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Stock Transfer Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `StockTransfers`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocEntry+LineNum`. Child source: `StockTransferLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | No sample rows |
| `item_code` | `ItemCode` | `copy` | No sample rows |
| `warehouse` | `FromWarehouseCode` | `copy` | No sample rows |
| `voucher_no` | `DocNum` | `copy` | No sample rows |
| `voucher_detail_no` | `LineNum` | `copy` | No sample rows |
| `project` | `ProjectCode` | `copy` | No sample rows |
| `stock_uom` | `UoMCode` | `copy` | No sample rows |
| `actual_qty` | `Quantity` | `multiply` | No sample rows |
| `voucher_type` | `constant` | `constant` | Derived |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Stock Transfer Receipt Detail

- ERP DocType: `Stock Ledger Entry`. SAP endpoint: `StockTransfers`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `DocEntry+LineNum`. Child source: `StockTransferLines`.
- Filter: `none`.
- Sync: full snapshot; fetch only: yes; parent row limit: 20.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `posting_date` | `DocDate` | `sap_date` | No sample rows |
| `item_code` | `ItemCode` | `copy` | No sample rows |
| `warehouse` | `WarehouseCode` | `copy` | No sample rows |
| `voucher_no` | `DocNum` | `copy` | No sample rows |
| `voucher_detail_no` | `LineNum` | `copy` | No sample rows |
| `project` | `ProjectCode` | `copy` | No sample rows |
| `stock_uom` | `UoMCode` | `copy` | No sample rows |
| `actual_qty` | `Quantity` | `multiply` | No sample rows |
| `voucher_type` | `constant` | `constant` | Derived |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

## SAP Supplier

- ERP DocType: `Supplier`. SAP endpoint: `BusinessPartners`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `CardCode`. Child source: `none`.
- Filter: `CardType eq 'cSupplier'`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `supplier_name` | `CardName` | `copy` | Value seen |
| `supplier_type` | `CompanyPrivate` | `value_map` | Value seen |
| `supplier_group` | `GroupCode` | `value_map` | Value seen |
| `country` | `Country` | `lookup` | Value seen |
| `tax_id` | `FederalTaxID` | `copy` | Value seen |
| `website` | `Website` | `copy` | Field seen, no value |
| `supplier_details` | `Notes` | `copy` | Field seen, no value |
| `default_currency` | `Currency` | `copy` | Value seen |
| `default_price_list` | `PriceListNum` | `lookup Price List.name` | Value seen |
| `disabled` | `Frozen` | `conditional_value` | Value seen |
| `is_frozen` | `Frozen` | `value_map` | Value seen |
| `mobile_no` | `Cellular` | `copy` | Field seen, no value |
| `email_id` | `EmailAddress` | `copy` | Field seen, no value |

SAP Supplier group `101` maps to ERP group `101`. SAP group `102` is `Services` and maps to the already existing ERP `Services` group; creating a second group with the same unique name would fail. The source group `102` itself stays in raw staging. Thirty Suppliers have a broken group link, and 74 have a broken Price List link. One Supplier has a Currency link with no ERP record. The group and Price List links are in [the master link repair review](sap-master-link-repair-review.md).

## SAP Supplier Group

- ERP DocType: `Supplier Group`. SAP endpoint: `BusinessPartnerGroups`.
- Live state: enabled. Sample: 2 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `Type eq 'bbpgt_VendorGroup'`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `supplier_group_name` | `Name` | `copy` | Value seen |

SAP code `102` has the name `Services`, which is already used by an ERP Supplier Group. Its source row stays in staging. Supplier records can reference the existing `Services` group directly.

## SAP Supplier Quotation

- ERP DocType: `Supplier Quotation`. SAP endpoint: `PurchaseQuotations`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `DocEntry`. Child source: `none`.
- Filter: `none`.
- Sync: date filtered; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`, `status`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `transaction_date` | `DocDate` | `sap_date` | Value seen |
| `currency` | `DocCurrency` | `copy` | Value seen |
| `conversion_rate` | `DocRate` | `copy` | Value seen |
| `items.item_code` | `ItemCode` | `copy` | Value seen |
| `items.item_name` | `ItemDescription` | `copy` | Value seen |
| `items.description` | `ItemDescription` | `copy` | Value seen |
| `items.qty` | `Quantity` | `copy` | Value seen |
| `items.rate` | `Price` | `copy` | Value seen |
| `items.warehouse` | `WarehouseCode` | `copy` | Value seen |
| `items.amount` | `LineTotal` | `copy` | Value seen |
| `items.discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `items.uom` | `UoMCode` | `copy` | Value seen |
| `supplier` | `CardCode` | `copy` | Value seen |
| `supplier_name` | `CardName` | `copy` | Value seen |
| `grand_total` | `DocTotal` | `copy` | Value seen |
| `docstatus` | `Cancelled` | `conditional_value` | Value seen |
| `additional_discount_percentage` | `DiscountPercent` | `copy` | Value seen |
| `discount_amount` | `TotalDiscount` | `copy` | Value seen |
| `project` | `Project` | `copy` | Field seen, no value |

## SAP Territory

- ERP DocType: `Territory`. SAP endpoint: `Territories`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `TerritoryID`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `territory_name` | `Description` | `copy` | Value seen |

## SAP UOM

- ERP DocType: `UOM`. SAP endpoint: `UnitOfMeasurements`.
- Live state: enabled. Sample: 3 parent rows. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `uom_name` | `Code` | `copy` | Value seen |

## SAP Warehouse

- ERP DocType: `Warehouse`. SAP endpoint: `Warehouses`.
- Live state: enabled. Sample: 2 parent rows. Response: `ok`.
- SAP ID: `WarehouseCode`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `warehouse_name` | `WarehouseName` | `copy` | Value seen |
| `company` | `configuration context` | `context` | Derived |
| `disabled` | `Inactive` | `value_map` | Value seen |
| `account` | `StockAccount` | `copy` | Field seen, no value |
| `address_line_1` | `Street` | `copy` | Field seen, no value |
| `address_line_2` | `BuildingFloorRoom` | `copy` | Field seen, no value |
| `city` | `City` | `copy` | Field seen, no value |
| `state` | `State` | `copy` | Field seen, no value |
| `pin` | `ZipCode` | `copy` | Field seen, no value |

## SAP Warehouse Location

- ERP DocType: `Location`. SAP endpoint: `WarehouseLocations`.
- Live state: disabled. Sample: 1 parent row. Response: `ok`.
- SAP ID: `Code`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: none.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `location_name` | `Name` | `copy` | Value seen |

## SAP Work Order

- ERP DocType: `Work Order`. SAP endpoint: `ProductionOrders`.
- Live state: enabled. Sample: 0 parent rows. Response: `ok`.
- SAP ID: `AbsoluteEntry`. Child source: `none`.
- Filter: `none`.
- Sync: full snapshot; fetch only: no; parent row limit: none.
- Required ERP fields without a direct mapping: `naming_series`, `bom_no`. Check site row rules and defaults.

| ERP field | SAP source | Transform | Sample status |
| --- | --- | --- | --- |
| `company` | `configuration context` | `context` | Derived |
| `production_item` | `ItemNo` | `copy` | No sample rows |
| `qty` | `PlannedQuantity` | `copy` | No sample rows |
| `fg_warehouse` | `Warehouse` | `copy` | No sample rows |
| `planned_start_date` | `StartDate` | `sap_date` | No sample rows |
| `required_items.item_code` | `ItemNo` | `copy` | No sample rows |
| `required_items.required_qty` | `PlannedQuantity` | `copy` | No sample rows |
| `required_items.source_warehouse` | `Warehouse` | `copy` | No sample rows |
| `docstatus` | `ProductionOrderStatus` | `value_map` | No sample rows |

No parent rows were available in the sampled response. Verify this mapping when SAP has matching records.

