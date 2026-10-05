"""
Patch: backfill_last_synced_on

The sync workers set `last_synced_on = external.modified` after a successful
sync.  However, a bug in several sync functions called `db_set("remote_id")`
without `update_modified=False` immediately before writing `last_synced_on`.
That bumped `modified` by a second or two, so the condition::

    modified > last_synced_on

became true again immediately after every sync run, causing
`get_external_doc_names_to_sync()` to return every previously-synced record
as "pending" on the next scheduler tick (8 000+ records in the case of
External Material Request).

The underlying write-order bug has been fixed in the sync functions.
This patch repairs the historical data by bulk-setting::

    last_synced_on = modified

for every External staging record that:

* has already been synced at least once (`remote_id IS NOT NULL`), AND
* is currently "dirty" (`last_synced_on IS NULL OR modified > last_synced_on`)

Records that have never been synced (`remote_id IS NULL`) are left untouched
so they will be picked up normally on the next scheduler run.
"""

import frappe

# Every External staging doctype that has both a `last_synced_on` and a
# `remote_id` field.  Add / remove entries here if the schema changes.
EXTERNAL_DOCTYPES = [
    "External Request for Quotation",
]


def execute():
    """Entry point called by ``bench migrate``."""
    updated_by_doctype = {}

    for doctype in EXTERNAL_DOCTYPES:
        if not frappe.db.exists("DocType", doctype):
            continue

        meta = frappe.get_meta(doctype)
        if not meta.has_field("last_synced_on") or not meta.has_field("remote_id"):
            continue

        # Single bulk UPDATE — no per-row Python loop needed.
        # Sets last_synced_on = modified for records that are synced but dirty.
        frappe.db.sql(
            f"""
            UPDATE `tab{doctype}`
            SET    `last_synced_on` = `modified`
            WHERE  `remote_id` IS NOT NULL
              AND  `remote_id` != ''
              AND  (
                       `last_synced_on` IS NULL
                    OR `modified` > `last_synced_on`
                   )
            """,
        )

        count = frappe.db.sql("SELECT ROW_COUNT()")[0][0]

        if count:
            updated_by_doctype[doctype] = count


    total = sum(updated_by_doctype.values())
    detail = "\n".join(f"  {dt}: {n}" for dt, n in sorted(updated_by_doctype.items()))
    frappe.log_error(
        title="backfill_last_synced_on complete",
        message=(
            f"Total records updated: {total}\n\n"
            f"Breakdown:\n{detail or '  (none)'}"
        ),
    )
