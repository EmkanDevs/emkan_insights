# Emkan Insights development

## Sites and scope

- Use `sapinsight.emkan-dev.com` for the current SAP integration data.
- Use `test.emkan-dev.com` for focused tests.
- Treat `insight.emkan-dev.com` as a separate site. Confirm the site before reading or changing data.
- Pass `--site <site>` to every bench command.
- Keep changes in this app. Do not overwrite unrelated working tree changes.

## SAP Account hierarchy

- The SAP Account mapping converts `FatherAccountKey` to an ERPNext Account name in the same company.
- ERPNext Account links use document names. Do not store SAP account codes in `parent_account`.
- Prefix imported Account names and links with the company abbreviation when the configuration requires it.
- Preserve the five ERPNext company root names, such as `Asset - S`. Do not turn them into `S-Asset - S`.
- Check that each parent exists, belongs to the same company, is a group Account, and has the child's root type.
- After changing Account parents, rebuild the nested-set tree and check every `lft` and `rgt` interval.
- Back up the site database before changing live Account links or tree intervals.

## Samif repair

- The importer fix is in `emkan_insights/emkan_insights/external_data_bulk_insert.py`.
- The data repair is `emkan_insights.patches.v0_0_1.repair_samif_account_roots`.
- The repair maps only malformed Samif links to the five existing company roots.
- It stops if it finds another missing parent or a root that fails validation.
- The repair ran on `sapinsight.emkan-dev.com` on 2026-11-02 after a database backup.
- Check the current data before you rerun the repair or sync Accounts.

## Other tree DocTypes

- Preserve the ERPNext company root Cost Center name, such as `Samif - S`, during import.
- Check imported tree parents against live document names before a sync.
- Check whether a mapping supplies parent fields before expecting a hierarchy. An empty parent field makes a flat tree.
- Check existing document names before changing `abbr_required`. A new prefix can create duplicate records on resync.
- For SAP Warehouse resync, match the SAP ID to an existing Warehouse in the configured company.
- Preserve an existing Warehouse name. Use the configured prefix only for a new Warehouse.
- Stop the sync if prefixed and unprefixed Warehouse names both match one SAP ID.

## Verification

- Run `bench --site test.emkan-dev.com run-tests --module emkan_insights.emkan_insights.test_external_data_bulk_insert` for importer changes.
- Check all Account parent links and tree intervals after a hierarchy repair.
- Check the SAP Account mapping and staging holds before a full sync.
