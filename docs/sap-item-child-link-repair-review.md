# SAP Item child link repair review

Site: `sapinsight.emkan-dev.com`. Read-only audit: 2026-10-23.

The [change manifest](sap-item-child-link-repair-review.json) lists 11 exact child-row changes:

| Field | Rows | Proposed value |
| --- | ---: | --- |
| Item Supplier.supplier | 3 | Existing ERP Supplier code matching the saved name |
| Item Default.default_supplier | 3 | Existing ERP Supplier code matching the saved name |
| Item Group Item Default.default_warehouse | 5 | Clear the invalid SAP enum `bomcw_BOM` |

No live child row was changed by this review. The future source mappings use Supplier codes and no longer copy the SAP component-warehouse enum into an ERP Warehouse Link. The affected source records stay in raw staging. Three Items and one Material Request line also reference a UOM named `exce`, which does not exist in ERP; that case needs a separate mapping decision.
