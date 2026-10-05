"""
Patch: fix_double_company_prefix

Any External staging doctype whose sync function prepended company_abbr to a
name that was already company-prefixed produced double-prefixed target documents
(e.g. CPC-CPC-RFQ-2025-00107). This patch renames every affected document to
the correct single-prefix name and fixes the staging record's remote_id field.

Add every affected (external_doctype, target_doctype, remote_id_field) triple
to MAPPINGS before running.
"""

import frappe

# (External staging doctype, actual doctype, remote_id field on staging record)
MAPPINGS = [
    ("External Request for Quotation", "Request for Quotation", "remote_id"),
    # ("External Material Request", "Material Request", "remote_id"),
    # ("External Purchase Order",   "Purchase Order",   "remote_id"),
]


def execute():
    abbrs = frappe.db.sql_list(
        "SELECT DISTINCT abbr FROM tabCompany WHERE abbr IS NOT NULL AND abbr != ''"
    )
    if not abbrs:
        return

    for external_dt, target_dt, remote_field in MAPPINGS:
        if not frappe.db.exists("DocType", target_dt):
            continue

        # ponytail: check once per mapping, not once per document
        has_remote_field = (
            frappe.db.exists("DocType", external_dt)
            and frappe.get_meta(external_dt).has_field(remote_field)
        )

        renamed = skipped = 0

        for abbr in abbrs:
            for old_name in frappe.db.sql_list(
                f"SELECT name FROM `tab{target_dt}` WHERE name LIKE %s",
                (f"{abbr}-{abbr}-%",),
            ):
                new_name = old_name[len(abbr) + 1:]

                if frappe.db.exists(target_dt, new_name):
                    frappe.log_error(
                        title=f"double-prefix patch SKIPPED: {old_name}",
                        message=f"'{old_name}' → '{new_name}' blocked: target exists. Manual review required.",
                    )
                    skipped += 1
                    continue

                frappe.rename_doc(target_dt, old_name, new_name, force=True)

                if has_remote_field:
                    frappe.db.sql(
                        f"UPDATE `tab{external_dt}` SET `{remote_field}` = %s WHERE `{remote_field}` = %s",
                        (new_name, old_name),
                    )

                renamed += 1
                if not renamed % 50:
                    print(f"{renamed} records renamed")

        frappe.log_error(
            title=f"double-prefix patch complete: {target_dt}",
            message=f"Renamed: {renamed}   Skipped (collision): {skipped}",
        )
