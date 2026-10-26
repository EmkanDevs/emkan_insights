# SAP Item Group link repair review

Site: `sapinsight.emkan-dev.com`. Read-only audit: 2026-10-23.

The ERP Item Group records use SAP numeric codes as their record names. Earlier Item imports saved the descriptive group name as the `item_group` link. A read-only comparison found 414 Items with missing Item Group links. Each descriptive name matches exactly one existing Item Group record. The proposed targets are `104` (268 Items), `107` (123), `108` (16), and `109` (7). See [the exact change list](sap-item-group-repair-review.json).

The user approved the exact change list. A database backup completed at 2026-10-23 14:25:33 before the repair. All 414 reviewed `Item.item_group` links were updated in one transaction. A post-change query found zero unresolved Item Group links. The SAP Item mapping now uses `ItemsGroupCode` to look up the ERP Item Group record name. Sampled Stock Entry Issue and Stock Entry Receipt documents passed ERPNext validation after this repair. Other Item and partner links have separate findings in the [mapping audit](sap-doctype-mapping-audit.md).
