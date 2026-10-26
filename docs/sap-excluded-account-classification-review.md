# SAP excluded Account classification review

Site: `sapinsight.emkan-dev.com`. SAP company: `EMKAN_LIVE_PBI`. Source check: 2026-10-26.

SAP has 845 Chart of Accounts records. The ERP Account mapping holds 72 records with prefixes `7`, `9`, and `A`. The Account fetch saved all 72 as raw External Account rows. It did not create ERP Accounts for them.

The SAP `7` root is `Non-Operating Income and Expenditure`. It mixes income and expense branches. ERPNext needs separate Income and Expense roots, so this SAP root cannot map to one ERP Account with the same parent relationships.

| SAP branch | SAP name | Group records | Active leaves | Proposed ERP root | Review reason |
| --- | --- | ---: | ---: | --- | --- |
| `71` | Finance Expenses | 5 | 10 | Expense, Profit and Loss | SAP labels every leaf `at_Expenses`. |
| `72` | Minority Interest | 3 | 1 | Unresolved | Confirm whether SAP reports this in equity or profit and loss. |
| `73` | Other Non-Operating Expenses | 3 | 1 | Expense, Profit and Loss | SAP branch name identifies expenses. |
| `74` | Extra-Ordinary Expenses | 3 | 13 | Expense, Profit and Loss | Some leaf names describe gains. Confirm the report placement. |
| `78` | Miscellaneous Revenues | 3 | 13 | Income, Profit and Loss | SAP branch name identifies revenues. |
| `79` | Taxation | 3 | 4 | Expense, Profit and Loss | Confirm tax account presentation. |
| `9` | Transition Account | 4 | 4 | Unresolved | Includes Other Comprehensive Income and opening balance accounts. |
| `A` | `#10` | 1 | 0 | Unresolved | The root has no active leaf in this snapshot. |

The existing ERP chart has `Expenses - E`, `Income - E`, and `Equity - E` roots. Any approved import must place each SAP branch under a compatible ERP root. It must also preserve each SAP account number and keep the source parent code for audit.

The accounting owner must confirm the proposed branches and decide `72`, `9`, and `A`. The SAP Account sync remains held for these records. The ten missing Journal Entry account codes are among the held `7` and `9` records.
