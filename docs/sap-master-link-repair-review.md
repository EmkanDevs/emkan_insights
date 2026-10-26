# SAP master link repair review

Site: `sapinsight.emkan-dev.com`. Read-only audit: 2026-10-23.

The earlier SAP mappings stored descriptive labels in ERP Link fields whose record names are SAP codes. A read-only comparison found 228 broken links with one matching ERP record for each label. The exact old and proposed values are in [the change manifest](sap-master-link-repair-review.json).

| ERP field | Proposed changes |
| --- | ---: |
| Customer.customer_group | 5 |
| Customer.default_price_list | 5 |
| Supplier.supplier_group | 30 |
| Supplier.default_price_list | 74 |
| Item.asset_category | 114 |

No live Customer, Supplier, or Item values were changed by this review. The source mappings now use ERP record names. Existing affected records and dependent transactions stay in raw staging. A separate decision is needed for three Items with missing ERP UOM records and one Supplier with a missing ERP Currency record; this manifest does not include them.
