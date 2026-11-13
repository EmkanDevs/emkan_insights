"""Repair staged SAP rows without refetching a full snapshot.

Mapping errors are stored in the staging JSON when a record is fetched, so fixing a mapper
or filling a staging gap does not repair rows already staged. A cursor or snapshot refetch
would pull hundreds of thousands of unrelated records. These helpers refetch only the SAP
documents that failed, then sync them.
"""
from __future__ import annotations

import time

import frappe

from emkan_insights.sap.services import get_mapped_data
from emkan_insights.sap.sync import sync_mapping

JOURNAL_ERROR = "%no ERPNext Journal Entry%"
ERRORS = "%_sap_mapping_errors%"


def staged_error_ids(mapping_name: str, configuration: str, like: str = ERRORS) -> list[int]:
	"""SAP keys (first part of the ID field) of staged rows whose JSON matches like."""
	mapping = frappe.get_doc("SAP Doctype Mapping", mapping_name)
	prefix = mapping.sap_id_prefix or ""
	rows = frappe.db.sql(
		f"SELECT DISTINCT SUBSTRING_INDEX(SUBSTRING(remote_id, %s), ':', 1) "
		f"FROM `tab{mapping.external_doctype}` "
		"WHERE external_sap_configuration = %s AND `json` LIKE %s",
		(len(prefix) + 1, configuration, like),
	)
	return sorted(int(key) for (key,) in rows if str(key).isdigit())


def missing_journal_ids(configuration: str) -> list[int]:
	"""Journals that staged GL rows point to but that are not in Journal Entry staging."""
	wanted = set(staged_error_ids("SAP GL Journal Detail", configuration, JOURNAL_ERROR))
	if not wanted:
		return []
	staged = {
		int(key) for (key,) in frappe.db.sql(
			"SELECT remote_id FROM `tabExternal Journal Entry` WHERE external_sap_configuration = %s",
			(configuration,),
		) if str(key).isdigit()
	}
	return sorted(wanted - staged)


def _row_name(configuration: str, mapping_name: str) -> str:
	return frappe.db.get_value(
		"External Site Configuration CT",
		{"parent": configuration, "parenttype": "External SAP Configuration", "sap_mapping": mapping_name},
		"name",
	)


def _clean_ids(mapping_name: str, remote_ids: list[str], configuration: str) -> set[str]:
	"""remote_ids from a refetch whose staged rows no longer carry mapping errors."""
	table = frappe.get_doc("SAP Doctype Mapping", mapping_name).external_doctype
	clean: set[str] = set()
	for start in range(0, len(remote_ids), 2000):
		chunk = remote_ids[start:start + 2000]
		rows = frappe.db.sql(
			f"SELECT remote_id FROM `tab{table}` WHERE external_sap_configuration = %s "
			"AND remote_id IN %s AND `json` NOT LIKE %s",
			(configuration, chunk, ERRORS),
		)
		clean.update(remote_id for (remote_id,) in rows)
	return clean


def refetch(configuration: str, mapping_name: str, ids: list[int], *, sync: bool = True) -> dict:
	"""Refetch the documents with these SAP keys, then sync only the rows that now map cleanly.

	Rows that still fail stay staged with their errors, so one bad row cannot block the rest.
	"""
	if not ids:
		return {"mapping": mapping_name, "ids": 0}
	started = time.monotonic()
	result = get_mapped_data(mapping_name, configuration_name=configuration, only_ids=ids)
	frappe.db.commit()
	fetched = time.monotonic() - started
	clean = _clean_ids(mapping_name, result["success"], configuration)
	summary = {
		"mapping": mapping_name, "ids": len(ids), "rows": len(result["success"]),
		"created": len(result["created"]), "updated": len(result["updated"]),
		"unchanged": len(result["unchanged"]), "still_failing": len(result["success"]) - len(clean),
		"fetch_seconds": round(fetched, 1),
	}
	if sync and clean:
		summary["sync"] = sync_mapping(_row_name(configuration, mapping_name), remote_ids=clean)
		summary["sync_seconds"] = round(time.monotonic() - started - fetched, 1)
	return summary


BATCH = 3000


def _unsynced_ids(mapping_name: str, configuration: str) -> list[int]:
	"""Keys of clean staged rows that were repaired but not synced yet."""
	mapping = frappe.get_doc("SAP Doctype Mapping", mapping_name)
	prefix = mapping.sap_id_prefix or ""
	rows = frappe.db.sql(
		f"SELECT DISTINCT SUBSTRING_INDEX(SUBSTRING(remote_id, %s), ':', 1) "
		f"FROM `tab{mapping.external_doctype}` WHERE external_sap_configuration = %s "
		"AND last_synced_on IS NULL AND `json` NOT LIKE %s",
		(len(prefix) + 1, configuration, ERRORS),
	)
	return sorted(int(key) for (key,) in rows if str(key).isdigit())


def _batched(configuration: str, mapping_name: str, ids: list[int]) -> list[dict]:
	results = []
	for start in range(0, len(ids), BATCH):
		summary = refetch(configuration, mapping_name, ids[start:start + BATCH])
		print(summary, flush=True)
		results.append(summary)
	return results


def repair_journal_gaps(configuration: str) -> list[dict]:
	"""Backfill Journal Entries that GL and payment rows need, then refetch those GL rows.

	Order matters: the GL mapping looks up the ERPNext Journal Entry while it maps.
	"""
	results = _batched(configuration, "SAP Journal Entry", missing_journal_ids(configuration))
	gl_ids = sorted(
		set(staged_error_ids("SAP GL Journal Detail", configuration, JOURNAL_ERROR))
		| set(_unsynced_ids("SAP GL Journal Detail", configuration))
	)
	return results + _batched(configuration, "SAP GL Journal Detail", gl_ids)


def repair_staged_errors(configuration: str, mapping_names: list[str]) -> list[dict]:
	"""Refetch and sync the documents of each mapping that still have staged mapping errors."""
	results = []
	for name in mapping_names:
		results += _batched(configuration, name, staged_error_ids(name, configuration))
	return results
