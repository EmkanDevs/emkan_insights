"""
Shared fail-fast helpers for the external sync pipeline.

Contract for the sequential SYNC_SEQUENCE pipeline:
- A record that syncs successfully is collected and the loop continues.
- The first record failure stops everything: no further records in the
  current chunk, no further chunks, no further doctypes.
- The failure is logged once with context and the ORIGINAL exception is
  re-raised so it propagates to the top-level sequential scheduler.
- Transaction completion is owned by the Frappe request or job runner.

This module never swallows exceptions and never converts a failure into a
successful return value. It also introduces no locking of its own: pass the
module's existing per-record context manager (savepoint / sync_record_guard)
via `record_context`, or none at all.
"""

import frappe


def log_record_failure(source_doctype, name, error=None):
	"""Log one failed record with full context (called right before re-raise)."""
	title = f"{source_doctype or 'External'} Sync FAILED: {name}"[:140]
	message = (
		f"Source DocType: {source_doctype or 'N/A'}\n"
		f"Failed Record: {name}\n"
		f"Error: {error if error is not None else 'See traceback'}\n\n"
		f"{frappe.get_traceback()}"
	)
	frappe.log_error(title=title, message=message)


def sync_records_fail_fast(names, run_one, source_doctype=None, record_context=None):
	"""Run `run_one(name)` for every name; abort on the first failure.

	- record_context: optional callable(name) -> context manager implementing
	  the module's EXISTING per-record transaction/lock behavior (e.g.
	  sync_record_guard or a savepoint context). Its rollback-on-error and
	  release logic runs before the exception reaches this helper.
	"""
	results = []
	for name in names:
		try:
			if record_context is None:
				results.append(run_one(name))
			else:
				with record_context(name):
					results.append(run_one(name))
		except Exception as e:
			log_record_failure(source_doctype, name, str(e))
			raise
	return results


def run_chunks_fail_fast(names, chunk_size, run_chunk):
	"""Process records chunk-by-chunk synchronously (pipeline mode only).

	The first exception stops the loop. Remaining records are skipped and the
	exception propagates so Frappe can roll back the transaction.
	"""
	results = []
	for start in range(0, len(names), chunk_size):
		chunk = names[start:start + chunk_size]
		chunk_result = run_chunk(chunk)
		if isinstance(chunk_result, list):
			results.extend(chunk_result)
		elif chunk_result is not None:
			results.append(chunk_result)
	return results
