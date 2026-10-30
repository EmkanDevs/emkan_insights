# Create an SAP DocType Mapping

Use an SAP DocType Mapping to copy SAP Business One Service Layer data into ERPNext.

The mapping has two stages:

1. It fetches SAP records into an External DocType.
2. It creates or updates ERPNext documents from those staged records.

Only a System Manager can create or change a mapping.

## Before you start

Collect this information:

- The SAP Service Layer entity name, such as `Banks` or `BusinessPartners`.
- One sample SAP response from that entity.
- The SAP field that uniquely identifies each record.
- The target ERPNext DocType and its fieldnames.
- An External DocType for staging the records.
- Any OData filter needed to select the correct SAP records.

SAP entity names and fieldnames are case-sensitive.

The External DocType must contain these fields:

| Fieldname | Purpose |
| --- | --- |
| `remote_id` | Stores the SAP record ID. |
| `source_site` | Supports the common external-data format. |
| `external_sap_configuration` | Identifies the SAP connection. |
| `last_synced_on` | Records the last successful ERPNext sync. |
| `json` | Stores the raw or mapped JSON document. |

Use an existing External DocType when it matches the target. A new External DocType requires an administrator or developer.

## Recommended setup process

Test the mapping with a small sample before it can write to ERPNext.

### 1. Create a raw review mapping

1. Open **SAP DocType Mapping** from the search bar.
2. Select **New**.
3. Enter a unique **Mapping Name**.
4. Select **Enabled**.
5. Enter the SAP entity in **SAP Entity**.
6. Select the staging DocType in **External DocType**.
7. Select the destination in **ERPNext DocType**.
8. Select **Fetch Only**.
9. Select **Store Raw SAP Payload**.
10. Set **Fetch Limit** to `5`.
11. Configure the sync mode.
12. Enter the unique source field in **SAP ID Field**.
13. Save the mapping.

The raw mode does not require a Field Mapping. It never creates ERPNext documents.

### 2. Add the mapping to an SAP connection

1. Open the applicable **External SAP Configuration**.
2. Save the configuration to add all enabled mappings.
3. Confirm that the new mapping appears in the **Mappings** table.
4. Select **Check Connection**.
5. Select **Fetch Data** on the new mapping row.
6. Wait for **Fetch Completed At** to contain a value.
7. Open the selected External DocType.
8. Filter by the External SAP Configuration.
9. Review the `json` value in each staged record.

The raw JSON contains the source endpoint, mapping name, and SAP record.

### 3. Add and test the field mapping

1. Return to the SAP DocType Mapping.
2. Clear **Store Raw SAP Payload**.
3. Keep **Fetch Only** selected.
4. Enter the Field Mapping JSON.
5. Save the mapping.
6. Fetch the mapping again from the External SAP Configuration.
7. Review the transformed `json` in the External DocType.
8. Confirm that every key is a valid ERPNext fieldname.
9. Confirm that all required ERPNext fields have values.
10. Confirm that Link values already exist in ERPNext.

Saving validates the JSON, transforms, target fields, and child-table fields.

### 4. Enable the ERPNext sync

1. Clear **Fetch Only** after the staged JSON is correct.
2. Keep **Fetch Limit** during the first controlled sync.
3. Save the mapping.
4. Fetch the mapping again.
5. Review the created or updated ERPNext documents.
6. Set **Fetch Limit** to `0` after the test succeeds.

The Fetch Data action fetches SAP data and then syncs it to ERPNext.

> **Warning:** A full snapshot with no fetch limit runs deletion reconciliation. It deletes staged and ERPNext records that are missing from SAP.

Verify the SAP Filter and returned record count before you remove the fetch limit.

## Complete example

This example maps the SAP `Banks` entity to the ERPNext `Bank` DocType.

| Setting | Value |
| --- | --- |
| Mapping Name | `SAP Bank` |
| SAP Entity | `Banks` |
| External DocType | `External Bank` |
| ERPNext DocType | `Bank` |
| Full Snapshot Sync | Selected |
| SAP ID Field | `BankCode` |
| Field Mapping | See the JSON below. |

```json
{
  "bank_name": "BankName",
  "swift_number": "SwiftNo"
}
```

The JSON keys are ERPNext fieldnames. The JSON values are SAP fieldnames.

For example, SAP `BankName` becomes ERPNext `bank_name`.

## Mapping settings

| Setting | Required | Use |
| --- | --- | --- |
| Mapping Name | Yes | Enter a unique and clear name. |
| Enabled | No | Allow the mapping to run and appear on SAP configurations. |
| SAP Entity | Yes | Enter the Service Layer entity name. |
| External DocType | Yes | Select the staging DocType. |
| ERPNext DocType | Yes | Select the destination DocType. |
| SAP Filter | No | Enter an OData filter without `$filter=`. |
| Source Array Field | No | Split a parent array into separate staging records. |
| Fetch Only | No | Fetch and stage data without changing ERPNext documents. |
| Store Raw SAP Payload | No | Store the complete SAP record for review. |
| Fetch Limit | No | Limit the fetched records. Use `0` for all records. |
| Full Snapshot Sync | Conditional | Fetch every SAP record on every run. |
| SAP Creation Date Filter Field | Conditional | Enter the SAP creation date for incremental sync. |
| SAP Update Date Filter Field | Conditional | Enter the SAP update date for incremental sync. |
| SAP ID Field | Yes | Enter the unique SAP field, or a composite ID. |
| SAP ID Prefix | No | Separate records when mappings share a staging DocType. |
| ERPNext SAP ID Fieldname | No | Match existing ERPNext documents by a stored SAP ID. |
| Field Mapping | Usually | Define the ERPNext-first mapping JSON. |

### SAP Filter

Enter only the OData expression.

```text
CardType eq 'cCustomer'
```

The application combines this filter with the configured date filter.

### Full snapshot and incremental sync

Select **Full Snapshot Sync** when SAP has no reliable creation or update date.

A full snapshot fetches every matching SAP record.

When **Fetch Limit** is `0`, it deletes staged and ERPNext records missing from the returned SAP snapshot.

An incorrect SAP Filter can therefore delete valid ERPNext records.

Clear **Full Snapshot Sync** to use incremental sync. Then enter at least one SAP date field.

The first incremental run has no automatic date range. Later runs use the last successful fetch date through the current date.

### SAP ID Field

The SAP ID must exist on every fetched record. The value identifies staged records and ERPNext documents.

Use `+` to build an ID from several SAP fields.

```text
CardCode+InternalCode
```

Composite IDs also retain parent fields when **Source Array Field** flattens an array.

### SAP ID Prefix

Use a prefix when several mappings share one External DocType.

For example, stock transfer records can use `transfer:`. Goods issue records can use `issue:`.

### ERPNext SAP ID Fieldname

Enter a target fieldname that stores the SAP ID. The field cannot be a child table.

The sync uses this field to update an existing ERPNext document. Without it, most mappings use the staged remote ID as the document name.

## Field Mapping format

The Field Mapping must be a JSON object. It can also be an array of JSON objects.

Each key must be an ERPNext fieldname. Each value defines where its value comes from.

### Direct field mapping

Use a string when the value needs no conversion.

```json
{
  "customer_name": "CardName",
  "tax_id": "FederalTaxID"
}
```

### Field mapping with a transform

Use an object when the value needs a transform.

```json
{
  "posting_date": {
    "fieldname": "DocDate",
    "transform": "sap_date"
  }
}
```

### Default value

The default applies when the transformed value is empty or null.

```json
{
  "country": {
    "fieldname": "Country",
    "default": "Saudi Arabia"
  }
}
```

The default must be a string, number, or Boolean value.

### Constant value

Use `constant` when every record needs the same value.

```json
{
  "stock_entry_type": {
    "transform": "constant",
    "transform_config": {
      "value": "Material Transfer"
    }
  }
}
```

### Company value

Use the connection context for the configured company.

```json
{
  "company": {
    "transform": "context",
    "transform_config": {
      "key": "company"
    }
  }
}
```

The context also provides `currency` for the company's default currency.

## Common transforms

### SAP date

`sap_date` keeps the first ten characters of a SAP date value.

```json
{
  "date_of_birth": {
    "fieldname": "DateOfBirth",
    "transform": "sap_date"
  }
}
```

Use `parent_date` inside a child mapping to read a date from its parent record.

### SAP Boolean

`sap_bool` maps `tYES`, `yes`, `y`, `true`, and `1` to `1`.

`sap_bool_not` returns the opposite Boolean value.

```json
{
  "disabled": {
    "fieldname": "Inactive",
    "transform": "sap_bool"
  }
}
```

### Value map

Use `value_map` to translate a known set of SAP values.

```json
{
  "customer_type": {
    "fieldname": "CompanyPrivate",
    "transform": "value_map",
    "transform_config": {
      "mapping": {
        "cCompany": "Company",
        "cPrivate": "Individual"
      },
      "strict": true
    }
  }
}
```

Set `strict` to `true` to reject unknown non-empty values. Set `normalize` to `lower` for case-insensitive matching.

Use `default` inside `transform_config` as the fallback value. Without it, an unknown value stays unchanged.

### ERPNext lookup

Use `lookup` to find an existing ERPNext document.

```json
{
  "debit_to": {
    "fieldname": "ControlAccount",
    "transform": "lookup",
    "transform_config": {
      "doctype": "Account",
      "field": "account_number",
      "return_field": "name",
      "company_context": true,
      "required": true
    }
  }
}
```

`return_field` defaults to `name`. `company_context` also filters by the configured company.

Set `required` to `true` when a missing reference must stop the record.

### First non-empty value

`first_value` uses the first non-empty SAP field.

```json
{
  "first_name": {
    "transform": "first_value",
    "transform_config": {
      "fields": ["FirstName", "Name"]
    }
  }
}
```

### Join fields

`join_fields` joins two or more SAP fields.

```json
{
  "account_name": {
    "transform": "join_fields",
    "transform_config": {
      "fields": ["Name", "Code"],
      "separator": " - ",
      "skip_missing": true
    }
  }
}
```

Set `skip_missing` to `false` when every source field is required.

### Conditional value

`conditional_value` checks conditions in order and returns the first matching value.

```json
{
  "disabled": {
    "transform": "conditional_value",
    "transform_config": {
      "conditions": [
        {
          "when": {"Frozen": "tYES"},
          "value": 1
        },
        {
          "when": {"Valid": "tNO"},
          "value": 1
        }
      ],
      "default": 0
    }
  }
}
```

A condition can use an exact value or an array of accepted values.

It can also use `in`, `not_in`, `starts_with`, or `not_empty`.

Set `strict` to `true` to reject a record when no condition matches.

### Numeric and text transforms

| Transform | Configuration | Result |
| --- | --- | --- |
| `sum_fields` | `fields` | Adds the configured SAP numeric fields. |
| `multiply` | `factor` | Multiplies the source value by a finite number. |
| `truncate` | `max_length` | Limits a string to a positive character count. |

## Child table mappings

Use a child mapping when the ERPNext target field is a Table or Table MultiSelect field.

### Map a SAP array

```json
{
  "items": {
    "fieldname": "DocumentLines",
    "mapping": {
      "item_code": "ItemCode",
      "qty": "Quantity",
      "rate": "Price",
      "warehouse": "WarehouseCode"
    }
  }
}
```

The outer key is the ERPNext table field. The `fieldname` is the SAP array field.

The nested mapping keys must be valid fields on the ERPNext child DocType.

### Create one row from the parent

```json
{
  "item_defaults": {
    "source": "parent",
    "mapping": {
      "company": {
        "transform": "context",
        "transform_config": {"key": "company"}
      },
      "default_warehouse": "DefaultWarehouse"
    },
    "required_fields": ["company"]
  }
}
```

### Create fixed rows

```json
{
  "uoms": {
    "rows": [
      {
        "uom": "InventoryUOM",
        "conversion_factor": {
          "transform": "constant",
          "transform_config": {"value": 1}
        }
      },
      {
        "uom": "PurchaseUnit",
        "conversion_factor": "PurchaseItemsPerUnit"
      }
    ],
    "required_fields": ["uom"],
    "unique_by": ["uom"],
    "positive_fields": ["conversion_factor"]
  }
}
```

Child mappings support these controls:

| Control | Result |
| --- | --- |
| `required_fields` | Skips a row when any listed field is empty. |
| `unique_by` | Removes identical duplicates and rejects conflicting duplicates. |
| `positive_fields` | Rejects a row unless each listed value is a positive finite number. |

## Advanced transforms

Use these transforms only when the target process needs them.

| Transform | Use |
| --- | --- |
| `sap_lookup` | Read one field from a unique record in another SAP entity. |
| `sap_exists` | Return `1` when a matching SAP record exists. |
| `item_conversion_factor` | Read the ERPNext item UOM conversion for a document line. |
| `payment_method_account` | Resolve the active SAP cash or transfer account. |
| `local_payment_value` | Build a verified local payment rate or amount. |
| `sap_account_amount` | Select the foreign or base account amount. |
| `sap_account_currency` | Use the SAP currency or the company default currency. |

Example SAP lookup:

```json
{
  "bank_name": {
    "fieldname": "BankCode",
    "transform": "sap_lookup",
    "transform_config": {
      "entity": "Banks",
      "key": "BankCode",
      "value_field": "BankName",
      "key_type": "string",
      "empty_values": ["-1"]
    }
  }
}
```

Use `key_type` values `string` or `integer`. SAP lookup identifiers can contain only letters, numbers, and underscores.

See [`mapping_catalog.json`](../emkan_insights/sap/mapping_catalog.json) for tested production examples.

## Dependencies and sync order

Create referenced master data before dependent records.

A common order is:

1. UOMs, banks, departments, territories, and sales persons.
2. Account groups and accounts.
3. Customer groups, supplier groups, and item groups.
4. Customers, suppliers, warehouses, and items.
5. Orders, receipts, invoices, payments, and journal entries.

A required lookup stops the record when its linked document does not exist.

Ledger DocTypes must use ERPNext submission to create ledger entries. Their mapped `docstatus` must start as `0`.

## Validate the result

Check these items after each test:

- **Fetch Started At** and **Fetch Completed At** contain recent times.
- The External DocType contains the expected `remote_id` values.
- The staged `json` keys match ERPNext fieldnames.
- Required Link values exist in ERPNext.
- Child rows contain the expected values and quantities.
- The target document count matches the SAP sample.
- A second fetch updates documents instead of creating duplicates.

Open **Error Log** when a fetch or sync fails. Search for titles starting with `SAP Fetch` or `SAP Document Sync`.

## Common errors

| Error | Cause | Fix |
| --- | --- | --- |
| Field mapping is not valid JSON | The JSON has a syntax error. | Check commas, quotes, and brackets. |
| ERPNext field does not exist | A JSON key is not a target fieldname. | Copy the fieldname from Customize Form. |
| Field must use a child mapping | A Table field uses a normal rule. | Add a `mapping` or `rows` child rule. |
| Child field must contain an array | The configured SAP child field is not an array. | Check the SAP sample and fieldname. |
| Incremental sync requires a date field | Both incremental date fields are empty. | Add a date field or use Full Snapshot Sync. |
| SAP record has no ID | The SAP ID Field is missing or empty. | Select a reliable ID or a composite ID. |
| ERPNext reference was not found | A required lookup found no target record. | Sync the required master data first. |
| SAP value has no equivalent | A strict value map has no matching key. | Add the source value to the map. |
| Synchronization is already running | The same mapping has an active job. | Wait for the current job to finish. |

## Change an existing mapping safely

1. Select **Fetch Only** before changing mapping rules.
2. Set a small **Fetch Limit**.
3. Save the mapping.
4. Fetch and review the staged JSON.
5. Clear **Fetch Only** after verification.
6. Run one controlled sync.
7. Remove the fetch limit after the sync succeeds.

Do not change a Mapping Name or SAP ID strategy after production sync without a migration plan. These values control record identity.
