# Historical SAP Journal Entry account review

Site: `sapinsight.emkan-dev.com`. Company: `Emkan`. Read-only check: 2026-10-26.

Run the check again after Account data changes:

```bash
bench --site sapinsight.emkan-dev.com execute emkan_insights.sap.journal_account_audit.audit_journal_accounts --args '["Emkan"]'
```

The site has 2,562 Journal Entries and 13,020 Journal Entry Account lines. No line has a valid ERP Account link. The Account table resolves 12,937 lines by account number. Ten missing account numbers occur on the other 83 lines. All ten start with `7` or `9`.

On 2026-10-26, the SAP Account fetch read all 845 Chart of Accounts records. It created 72 held raw External Account rows for the excluded `7,9,A` prefixes. All ten missing Journal Entry account numbers now have staged source records. The ERP Account count stayed at 854.

| SAP account number | Journal lines | Journal Entries |
| --- | ---: | ---: |
| 7111101 | 4 | 2 |
| 7112102 | 23 | 21 |
| 7113101 | 6 | 6 |
| 7311101 | 5 | 5 |
| 7411101 | 4 | 4 |
| 7411103 | 3 | 2 |
| 7911101 | 18 | 18 |
| 7911104 | 3 | 3 |
| 911100 | 14 | 14 |
| 911400 | 3 | 3 |

Of the 13,020 lines, 12,983 have zero local debit and credit but a nonzero amount in account currency fields. For example, account `7111101` has lines with zero local credit and nonzero credit in account currency. These submitted documents do not show the source amounts in their local debit and credit fields. The site also has no GL Entries.

SAP returned each missing Account and its parent chain. All ten use SAR. Eight `7` accounts sit under the `Non-Operating Income and Expenditure` root. Four have SAP type `at_Expenses`; four have `at_Other`. Two `9` accounts sit under the `#9` root. SAP marks the `9` root and both leaves as `at_Other`. The full `7` tree also includes income branches, so a single root type for the whole tree would be wrong.

The full SAP Chart of Accounts has 845 records. The 72 excluded records comprise 63 codes under `7`, eight under `9`, and one `A` root. The `A` root has no active leaf in this source snapshot. No excluded Account was posted to ERPNext during the fetch.

The [excluded Account classification review](sap-excluded-account-classification-review.md) lists the SAP branches and proposed ERP roots. The accounting owner must confirm them before their Account records can sync. The `9` transition and other comprehensive income accounts need explicit treatment. After classification, repair the ERP Account hierarchy and post accounting documents through ERPNext. Compare posted GL balances with SAP before using the ERPNext Balance Sheet report as evidence.
