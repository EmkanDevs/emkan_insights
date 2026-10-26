# SAP Account hierarchy repair review

Site: `sapinsight.emkan-dev.com`. Company: `Emkan`. This is a read-only review of the current ERP Account table.

The SAP Account mapping now resolves `FatherAccountKey` through ERP `Account.account_number`. This fixes the rule for future staged records. It does not change existing Account parent links. All newly fetched SAP Account rows receive a staging sync hold until the hierarchy repair is approved.

## Proposed live data change

The site has 854 ERP Accounts. Of these, 766 have a `parent_account` value that is a SAP code instead of an ERP Account name. All 766 codes identify one Account in the same company. The proposed repair changes 764 parent links. Each of those parents is a group Account with the same root type as its child.

After changing those links, the Account nested-set tree needs new `lft` and `rgt` values. The reviewed tree has 852 Accounts, 840 parent edges, 12 roots, and no cycles. Rebuilding it would change intervals on 827 Accounts. The [change manifest](sap-account-hierarchy-repair-review.json) lists every proposed parent link and interval.

Two SAP Accounts remain outside this repair. Their SAP `FatherAccountKey` resolves to an ERP group Account with a different root type. Their raw SAP responses are held in `External Account` staging. Their intended parent needs accounting review before their hierarchy can be corrected.

## Execution gate

Automatic approval review rejected the bulk hierarchy migration because it would change hundreds of live financial Account links and tree intervals. No bulk hierarchy update was executed. Before applying the reviewed change, take a database backup, confirm the manifest still matches live data, apply the changes in one transaction, check all parent links and nested-set intervals, and roll back if any check fails. The two root conflicts remain excluded.
