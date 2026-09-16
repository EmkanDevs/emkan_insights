"""Compare remote names with local records and remove missing imported records."""

from contextlib import nullcontext

import frappe

from emkan_insights.emkan_insights.external_data_bulk_insert import (
	_lock_key,
	_ordered_configuration_rows,
)
from emkan_insights.emkan_insights.external_data_deletion import (
	DELETION_LOCK_TTL,
	ExternalDataDeletionError,
	_delete_rows_with_children,
	_deletion_lock,
	_fetch_all_remote_ids,
	_get_batches,
	_get_context,
	_get_staging_rows,
	_get_target_name,
	_rebuild_tree_if_needed,
)


def execute(
	configuration_name: str,
	doctype: str | None,
	dry_run: bool = False,
	*,
	deletion_lock_acquired: bool = False,
) -> dict:
	"""Reconcile the selected DocType; use doctype=None for all configured DocTypes."""
	configuration = frappe.get_doc("External Site Configuration", configuration_name)
	rows = [
		row for row in reversed(_ordered_configuration_rows(configuration))
		if doctype is None or row.ref_doctype == doctype
	]
	if not rows:
		raise ExternalDataDeletionError("No matching configured DocTypes were found.")

	with nullcontext() if deletion_lock_acquired else _deletion_lock():
		cache = frappe.cache()
		key = _lock_key(configuration_name)
		if not cache.set(key, "1", nx=True, ex=DELETION_LOCK_TTL):
			raise ExternalDataDeletionError("Another fetch or sync is running for this configuration.")
		try:
			results = [_reconcile(_get_context(row.name), dry_run) for row in rows]
			if not dry_run:
				frappe.db.commit()
		except Exception:
			if not dry_run:
				frappe.db.rollback()
			raise
		finally:
			cache.delete(key)

	return {"status": "complete", "configuration": configuration_name, "dry_run": dry_run, "results": results}


def _reconcile(context, dry_run: bool) -> dict:
	remote_ids = _fetch_all_remote_ids(context)
	remote_names = {_get_target_name(context, name) for name in remote_ids}
	staging_rows = _get_staging_rows(context)
	local_names = _get_local_names(context, staging_rows)
	if not remote_ids and (local_names or staging_rows):
		raise ExternalDataDeletionError(
			f"{context.remote_doctype} returned an empty snapshot while local records exist."
		)

	missing_names = sorted(local_names - remote_names)
	missing_staging = sorted({
		name
		for remote_id, names in staging_rows.items()
		if _get_target_name(context, remote_id) not in remote_names
		for name in names
	})
	if not dry_run:
		for target_doctype, names in (
			(context.target_doctype, missing_names),
			(context.staging_doctype, missing_staging),
		):
			for batch in _get_batches(names):
				_delete_rows_with_children(target_doctype, batch)
			if names:
				_rebuild_tree_if_needed(target_doctype)

	return {
		"doctype": context.target_doctype,
		"remote": len(remote_names),
		"local": len(local_names),
		"missing": len(missing_names),
		"missing_external": len(missing_staging),
		"deleted": 0 if dry_run else len(missing_names),
		"deleted_external": 0 if dry_run else len(missing_staging),
		"missing_names": missing_names,
		"missing_external_names": missing_staging,
	}


def _configuration_names(context, local_names: set[str], staging_rows: dict) -> set[str]:
	if context.target_doctype in context.abbr_doctypes:
		if not context.company_abbr:
			raise ExternalDataDeletionError("A company abbreviation is required for prefixed DocTypes.")
		return {name for name in local_names if name.startswith(f"{context.company_abbr}-")}

	# Shared, unprefixed names need staging evidence to identify imported records.
	return local_names.intersection(staging_rows)


def _get_local_names(context, staging_rows: dict) -> set[str]:
	if context.target_doctype in context.abbr_doctypes:
		prefix = context.company_abbr.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
		names = frappe.get_all(
			context.target_doctype,
			filters={"name": ["like", f"{prefix}-%"]},
			pluck="name",
			limit_page_length=0,
		)
		return _configuration_names(context, set(names), staging_rows)

	names = set()
	for batch in _get_batches(list(staging_rows)):
		names.update(frappe.get_all(
			context.target_doctype,
			filters={"name": ["in", batch]},
			pluck="name",
			limit_page_length=0,
		))
	return names
