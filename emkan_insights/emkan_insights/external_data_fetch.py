import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote

import frappe
import requests
from frappe import _


PAGE_SIZE = 500
FETCH_WORKERS = 8
FETCH_LOCK_TTL = 7200
DEFERRED_NAMING_DOCTYPES = frozenset({"GL Entry", "Stock Ledger Entry"})


@dataclass(frozen=True)
class FetchContext:
	configuration: str
	row: str
	remote_doctype: str
	target_doctype: str
	site_url: str
	api_key: str
	api_secret: str
	window_start: str | None
	window_end: str


@frappe.whitelist(methods=["POST"])
def queue_external_data_fetch(child_docname: str) -> dict:
	context = _get_context(child_docname)
	frappe.has_permission(
		"External Site Configuration",
		"write",
		doc=context.configuration,
		throw=True,
	)
	_acquire_lock(context.row)

	try:
		frappe.enqueue(
			"emkan_insights.emkan_insights.external_data_fetch.fetch_external_data",
			queue="long",
			timeout=7200,
			enqueue_after_commit=True,
			job_name=f"external_data_fetch_{context.row}",
			child_docname=context.row,
			lock_acquired=True,
		)
	except Exception:
		_release_lock(context.row)
		raise

	return {
		"status": "queued",
		"message": _("Fetch queued for {0}.").format(context.remote_doctype),
	}


def fetch_external_data(child_docname: str, lock_acquired: bool = False) -> dict:
	context = _get_context(child_docname)
	if not lock_acquired:
		_acquire_lock(context.row)

	_mark_started(context)
	try:
		return _fetch_pages(context)
	except Exception:
		frappe.db.rollback()
		_log_failure(context)
		raise
	finally:
		_release_lock(context.row)


def _fetch_pages(context: FetchContext) -> dict:
	cursor = None
	fetched = 0

	while True:
		names = _fetch_remote_names(context, cursor)
		if not names:
			break

		documents, errors = _fetch_documents(context, names)
		if errors:
			raise ExternalDataFetchError(_format_fetch_errors(context, errors))

		_upsert_staging_rows(context, documents)
		fetched += len(documents)
		frappe.db.commit()
		cursor = names[-1]
		if len(names) < PAGE_SIZE:
			break

	return _mark_completed(context, fetched)


def _get_context(child_docname: str) -> FetchContext:
	row = frappe.db.get_value(
		"External Site Configuration CT",
		child_docname,
		["name", "parent", "ref_doctype", "exported_doctype", "last_sync"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("External Site Configuration row {0} was not found.").format(child_docname))
	if not row.ref_doctype:
		frappe.throw(_("External Site Configuration row {0} has no source DocType.").format(child_docname))

	configuration = frappe.get_doc("External Site Configuration", row.parent)
	_validate_configuration(configuration)
	_validate_staging_doctype(row.exported_doctype)
	return FetchContext(
		configuration=configuration.name,
		row=row.name,
		remote_doctype=row.ref_doctype,
		target_doctype=row.exported_doctype,
		site_url=(configuration.site_url or "").rstrip("/"),
		api_key=configuration.api_key,
		api_secret=configuration.api_secret,
		window_start=_as_datetime_string(row.last_sync),
		window_end=_as_datetime_string(frappe.utils.now_datetime()),
	)


def _validate_configuration(configuration) -> None:
	missing = [
		label
		for fieldname, label in (
			("site_url", _("Site URL")),
			("api_key", _("API Key")),
			("api_secret", _("API Secret")),
		)
		if not (configuration.get(fieldname) or "").strip()
	]
	if missing:
		frappe.throw(
			_("External Site Configuration {0} is missing: {1}.").format(
				configuration.name, ", ".join(missing)
			)
		)


def _validate_staging_doctype(doctype: str) -> None:
	if not doctype or not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Configured staging DocType {0} is not available.").format(doctype or ""))

	fieldnames = {field.fieldname for field in frappe.get_meta(doctype).fields}
	missing = {"remote_id", "source_site", "last_synced_on", "json"} - fieldnames
	if missing:
		frappe.throw(
			_("{0} is missing required staging fields: {1}.").format(
				doctype, ", ".join(sorted(missing))
			)
		)


def _fetch_remote_names(context: FetchContext, cursor: str | None) -> list[str]:
	filters = [["modified", "<=", context.window_end]]
	if context.window_start:
		filters.append(["modified", ">", context.window_start])
	if context.remote_doctype in DEFERRED_NAMING_DOCTYPES:
		filters.append(["to_rename", "=", 0])
	if cursor:
		filters.append(["name", ">", cursor])

	response = requests.get(
		f"{context.site_url}/api/resource/{quote(context.remote_doctype, safe='')}",
		headers=_headers(context),
		params={
			"fields": json.dumps(["name"]),
			"filters": json.dumps(filters),
			"limit_page_length": PAGE_SIZE,
			"order_by": "name asc",
		},
		timeout=90,
	)
	response.raise_for_status()
	return [row["name"] for row in response.json().get("data", []) if row.get("name")]


def is_deferred_naming_pending(doctype: str, document: dict) -> bool:
	return doctype in DEFERRED_NAMING_DOCTYPES and frappe.utils.cint(document.get("to_rename")) == 1


def _fetch_documents(context: FetchContext, names: list[str]) -> tuple[list[dict], list[dict]]:
	documents = []
	errors = []

	with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
		futures = {pool.submit(_fetch_document, context, name): name for name in names}
		for future in as_completed(futures):
			name = futures[future]
			try:
				documents.append(future.result())
			except Exception as error:
				errors.append({"remote_id": name, "error": str(error)})

	return documents, errors


def _fetch_document(context: FetchContext, name: str) -> dict:
	url = (
		f"{context.site_url}/api/resource/{quote(context.remote_doctype, safe='')}"
		f"/{quote(str(name), safe='')}"
	)
	response = requests.get(url, headers=_headers(context), timeout=60)
	response.raise_for_status()
	return response.json().get("data") or {"name": name}


def _upsert_staging_rows(context: FetchContext, documents: list[dict]) -> None:
	remote_ids = [str(document.get("name") or document.get("id") or "").strip() for document in documents]
	if any(not remote_id for remote_id in remote_ids):
		raise ExternalDataFetchError(f"{context.remote_doctype} returned a document without a name or id")

	existing = {
		row.remote_id: row.name
		for row in frappe.get_all(
			context.target_doctype,
			filters={
				"source_site": context.configuration,
				"remote_id": ["in", remote_ids],
			},
			fields=["name", "remote_id"],
			limit_page_length=0,
		)
	}
	now = frappe.utils.now_datetime()
	updates = {}
	new_documents = []
	for document, remote_id in zip(documents, remote_ids, strict=True):
		values = {
			"remote_id": remote_id,
			"source_site": context.configuration,
			"json": json.dumps(document, default=str, ensure_ascii=False),
		}
		if remote_id in existing:
			updates[existing[remote_id]] = values
		else:
			new_documents.append(_new_staging_row(context.target_doctype, values, now))

	if updates:
		frappe.db.bulk_update(context.target_doctype, updates, update_modified=True)
	if new_documents:
		frappe.db.bulk_insert(
			context.target_doctype,
			fields=list(new_documents[0]),
			values=[tuple(row.values()) for row in new_documents],
			chunk_size=PAGE_SIZE,
		)


def _new_staging_row(doctype: str, values: dict, now) -> dict:
	user = frappe.session.user or "Administrator"
	return {
		"name": frappe.generate_hash(length=10),
		"creation": now,
		"modified": now,
		"modified_by": user,
		"owner": user,
		"docstatus": 0,
		"idx": 0,
		**values,
	}


def _mark_started(context: FetchContext) -> None:
	frappe.db.set_value(
		"External Site Configuration CT",
		context.row,
		{"fetch_started_at": context.window_end, "fetch_completed_at": None},
		update_modified=False,
	)
	frappe.db.commit()


def _mark_completed(context: FetchContext, fetched: int) -> dict:
	completed_at = frappe.utils.now_datetime()
	frappe.db.set_value(
		"External Site Configuration CT",
		context.row,
		{
			"last_sync": context.window_end,
			"fetch_completed_at": completed_at,
		},
		update_modified=False,
	)
	result = {
		"status": "complete",
		"ref_doctype": context.remote_doctype,
		"exported_doctype": context.target_doctype,
		"count": fetched,
		"last_sync": context.window_end,
	}
	frappe.publish_realtime(
		"external_data_fetch_complete",
		{"child_docname": context.row, **result},
		after_commit=True,
	)
	return result


def _headers(context: FetchContext) -> dict:
	return {
		"Authorization": f"token {context.api_key}:{context.api_secret}",
		"Content-Type": "application/json",
	}


def _acquire_lock(child_docname: str) -> None:
	if not frappe.cache().set(_lock_key(child_docname), "1", nx=True, ex=FETCH_LOCK_TTL):
		frappe.throw(_("A fetch is already running for this configuration row."))


def _release_lock(child_docname: str) -> None:
	frappe.cache().delete(_lock_key(child_docname))


def _lock_key(child_docname: str) -> str:
	return f"external_data_fetch:{child_docname}"


def _as_datetime_string(value) -> str | None:
	return frappe.utils.get_datetime_str(value) if value else None


def _format_fetch_errors(context: FetchContext, errors: list[dict]) -> str:
	details = "; ".join(f"{row['remote_id']}: {row['error']}" for row in errors[:20])
	return f"Failed to fetch {len(errors)} {context.remote_doctype} document(s): {details}"


def _log_failure(context: FetchContext) -> None:
	traceback = frappe.get_traceback()
	frappe.logger("external_data_fetch").error(
		"External fetch failed for %s/%s: %s",
		context.configuration,
		context.remote_doctype,
		traceback,
	)
	frappe.log_error(title=f"External fetch failed: {context.remote_doctype}"[:140], message=traceback)


class ExternalDataFetchError(Exception):
	pass
