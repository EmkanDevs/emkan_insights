# SAP Blanket Order direction repair review

Site: `sapinsight.emkan-dev.com`. Read-only audit: 2026-10-23.

All nine live SAP Blanket Agreements reference ERP Suppliers, and none reference ERP Customers. The current ERP Blanket Orders put those supplier codes in `customer` and leave `supplier`, `blanket_order_type`, and `naming_series` empty. The corrected SAP mapping uses `supplier`, `Purchasing`, and the ERP Blanket Order naming series. A fresh SAP sample passed ERPNext validation and had no missing mandatory fields with this mapping.

The [change manifest](sap-blanket-order-repair-review.json) lists all nine exact ERP records and values. No existing Blanket Order was changed by this review. Their source rows stay in raw staging until the direction repair is approved and applied after a database backup.
