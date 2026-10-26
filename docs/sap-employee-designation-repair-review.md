# SAP Employee Designation link review

Site: `sapinsight.emkan-dev.com`. Read-only audit: 2026-10-23.

The old Employee mapping copied SAP `JobTitle` text into an ERP Designation Link. ERP Designation record names for SAP positions are numeric Position IDs. Of 39 existing Employees with broken Designation links, 29 job titles each match one existing ERP Designation record. The [manifest](sap-employee-designation-repair-review.json) lists the exact proposed changes. Ten job titles have no matching ERP Designation and need a separate mapping or a reviewed new Designation record.

No existing Employee was changed. The new mapping resolves a job title through `Designation.designation_name`; source rows without a match and Employees with a broken existing link stay in raw staging.
