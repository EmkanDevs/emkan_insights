# SAP and ERPNext balance comparison

Run this command from the bench directory after SAP Service Layer access works:

```bash
bench --site sapinsight.emkan-dev.com execute emkan_insights.sap.balance_reconciliation.compare_ledgers --args '["Emkan", "2026-10-26"]'
```

The command also reads journal headers separately and compares the ID sets. It raises an error if the two passes differ. To run the header check alone:

```bash
bench --site sapinsight.emkan-dev.com execute emkan_insights.sap.balance_reconciliation.verify_journal_pagination --args '["Emkan", "2026-10-26"]'
```

`sap_source_verified` is true only when the detailed and header journal ID sets match. A different digest means the two passes saw different SAP journal IDs.

Change the date to the SAP balance sheet posting date. The command reads all SAP `JournalEntries` through that date, including manual and document journals. It follows every SAP response page. It reads uncanceled ERPNext `GL Entry` rows for the same company and date. It groups both sources by SAP account number and compares debit less credit.

The result lists missing account links and every account balance difference. It places Asset, Liability, and Equity accounts in `balance_sheet_account_differences`. It places Income and Expense accounts in `other_account_differences`. `matched` is true only when SAP has journal lines, every account resolves, and every account balance matches.

This check uses posted ledger amounts. It does not treat submitted `Journal Entry` documents as posted GL. The SAP account hierarchy must also match ERPNext before the ERPNext Balance Sheet report can match SAP. Check the report dates, currency, fiscal year, and period closing rules separately.

The first live run on 2026-10-26 stopped at `$skip=1980` when Service Layer returned HTTP 500. A retry completed. The integrated two-pass check found the same 2,563 journal IDs in headers and details. The detailed pass read 13,022 journal lines and reported `sap_source_verified: true`. All 585 staged SAP leaf Account balances matched the journal debit less credit sums within 0.01 SAR.

| Result at 2026-10-26 | Count |
| --- | ---: |
| ERPNext GL Entry accounts | 0 |
| SAP Asset, Liability, and Equity account differences | 94 |
| SAP Income and Expense account differences | 66 |
| SAP account numbers without an ERP Account | 10 |

The ten unmatched SAP accounts have a combined net balance of 8,230,600.12 SAR. The live ERPNext site also has 2,562 submitted Journal Entries with invalid Account links. A submitted document does not create the missing GL Entries when it was inserted through the bulk writer. The ERPNext Balance Sheet cannot match SAP until the Account hierarchy and posting history are repaired.
