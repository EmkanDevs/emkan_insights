import json
from decimal import Decimal

import frappe
from frappe.utils import cint, get_datetime, getdate


PAGE_SIZE = 500
LEDGERS = {
	"GL Entry": {
		"staging_doctype": "External GL Entry",
		"fields": (
			"name",
			"company",
			"creation",
			"posting_date",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"account",
			"debit",
			"credit",
			"is_cancelled",
		),
	},
	"Stock Ledger Entry": {
		"staging_doctype": "External Stock Ledger Entry",
		"fields": (
			"name",
			"company",
			"creation",
			"posting_date",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"item_code",
			"warehouse",
			"actual_qty",
			"stock_value_difference",
			"is_cancelled",
		),
	},
}


def execute():
	plan = _build_cleanup_plan()
	results = {}
	for target_doctype, details in plan.items():
		staging_doctype = LEDGERS[target_doctype]["staging_doctype"]
		removed_target_rows = _delete_by_name(target_doctype, details["target_names"])
		removed_staging_rows = _delete_temporary_staging_rows(staging_doctype)
		results[target_doctype] = {
			"target_rows": removed_target_rows,
			"staging_rows": removed_staging_rows,
		}

	frappe.logger("external_sync").info("Temporary ledger cleanup complete: %s", results)


def get_cleanup_plan() -> dict[str, dict[str, int]]:
	return {
		target_doctype: {
			"target_rows": len(details["target_names"]),
			"staging_rows": details["staging_rows"],
		}
		for target_doctype, details in _build_cleanup_plan().items()
	}


def _build_cleanup_plan() -> dict[str, dict]:
	plan = {}
	for target_doctype, spec in LEDGERS.items():
		staging_doctype = spec["staging_doctype"]
		if not _tables_exist(target_doctype, staging_doctype):
			continue

		contexts = _get_source_contexts(target_doctype, staging_doctype)
		temporary_rows = _get_temporary_rows(staging_doctype, contexts)
		canonical_names = _get_canonical_names(staging_doctype, contexts)
		target_names = _find_obsolete_target_names(
			target_doctype,
			spec["fields"],
			temporary_rows,
			canonical_names,
		)
		plan[target_doctype] = {
			"target_names": target_names,
			"staging_rows": _count_temporary_staging_rows(staging_doctype),
		}
	return plan


def _tables_exist(target_doctype: str, staging_doctype: str) -> bool:
	return frappe.db.table_exists(target_doctype) and frappe.db.table_exists(staging_doctype)


def _get_source_contexts(target_doctype: str, staging_doctype: str) -> dict[str, dict]:
	rows = frappe.db.sql(
		"""
		SELECT configuration.name AS source_site,
			configuration.company,
			company.abbr,
			mapping.abbr_required
		FROM `tabExternal Site Configuration` configuration
		INNER JOIN `tabExternal Site Configuration CT` mapping
			ON mapping.parent = configuration.name
			AND mapping.ref_doctype = %s
			AND mapping.exported_doctype = %s
		LEFT JOIN `tabCompany` company ON company.name = configuration.company
		""",
		(target_doctype, staging_doctype),
		as_dict=True,
	)
	return {row.source_site: row for row in rows}


def _get_temporary_rows(staging_doctype: str, contexts: dict[str, dict]) -> list[dict]:
	if not contexts:
		return []

	rows = frappe.db.sql(
		f"""
		SELECT name, source_site, json
		FROM `tab{staging_doctype}`
		WHERE last_synced_on IS NOT NULL
			AND {_to_rename_sql()} = 1
		""",
		as_dict=True,
	)
	result = []
	for row in rows:
		if row.source_site not in contexts:
			continue
		document = json.loads(row.json) if isinstance(row.json, str) else row.json
		if isinstance(document, dict):
			result.append({"document": document, "context": contexts[row.source_site]})
	return result


def _get_canonical_names(staging_doctype: str, contexts: dict[str, dict]) -> set[str]:
	if not contexts:
		return set()

	rows = frappe.db.sql(
		f"""
		SELECT remote_id, source_site
		FROM `tab{staging_doctype}`
		WHERE remote_id IS NOT NULL
			AND remote_id != ''
			AND {_to_rename_sql()} = 0
		""",
		as_dict=True,
	)
	canonical_names = set()
	for row in rows:
		context = contexts.get(row.source_site)
		if context:
			canonical_names.add(_target_name(row.remote_id, context))
	return canonical_names


def _target_name(remote_id: str, context: dict) -> str:
	abbr = context.abbr or ""
	if cint(context.abbr_required) and abbr and not remote_id.startswith(f"{abbr}-"):
		return f"{abbr}-{remote_id}"
	return remote_id


def _find_obsolete_target_names(
	target_doctype: str,
	fields: tuple[str, ...],
	temporary_rows: list[dict],
	canonical_names: set[str],
) -> set[str]:
	if not temporary_rows:
		return set()

	companies = {row["context"].company for row in temporary_rows}
	creations = {
		get_datetime(row["document"].get("creation"))
		for row in temporary_rows
		if row["document"].get("creation")
	}
	candidates = []
	for creation_batch in _batches(list(creations), PAGE_SIZE):
		candidates.extend(
			frappe.get_all(
				target_doctype,
				filters={
					"company": ["in", list(companies)],
					"creation": ["in", creation_batch],
				},
				fields=list(fields),
				limit_page_length=0,
			)
		)

	rows_by_key = {}
	for row in temporary_rows:
		key = (row["context"].company, get_datetime(row["document"].get("creation")))
		rows_by_key.setdefault(key, []).append(row)

	obsolete_names = set()
	for candidate in candidates:
		if candidate.name in canonical_names:
			continue
		key = (candidate.company, get_datetime(candidate.creation))
		if any(
			_matches_temporary_row(target_doctype, candidate, temporary_row)
			for temporary_row in rows_by_key.get(key, [])
		):
			obsolete_names.add(candidate.name)
	return obsolete_names


def _matches_temporary_row(target_doctype: str, candidate, temporary_row: dict) -> bool:
	document = temporary_row["document"]
	abbr = temporary_row["context"].abbr or ""
	common_matches = (
		_as_text(candidate.voucher_type) == _as_text(document.get("voucher_type"))
		and _as_text(candidate.voucher_detail_no) == _as_text(document.get("voucher_detail_no"))
		and getdate(candidate.posting_date) == getdate(document.get("posting_date"))
		and cint(candidate.is_cancelled) == cint(document.get("is_cancelled"))
		and _matches_raw_or_prefixed(candidate.voucher_no, document.get("voucher_no"), abbr)
	)
	if not common_matches:
		return False

	if target_doctype == "GL Entry":
		return (
			_matches_raw_or_prefixed(candidate.account, document.get("account"), abbr)
			and _as_decimal(candidate.debit) == _as_decimal(document.get("debit"))
			and _as_decimal(candidate.credit) == _as_decimal(document.get("credit"))
		)
	return (
		_matches_raw_or_prefixed(candidate.item_code, document.get("item_code"), abbr)
		and _matches_raw_or_prefixed(candidate.warehouse, document.get("warehouse"), abbr)
		and _as_decimal(candidate.actual_qty) == _as_decimal(document.get("actual_qty"))
		and _as_decimal(candidate.stock_value_difference)
		== _as_decimal(document.get("stock_value_difference"))
	)


def _matches_raw_or_prefixed(target_value, source_value, abbr: str) -> bool:
	target = _as_text(target_value)
	source = _as_text(source_value)
	return target == source or bool(abbr and target == f"{abbr}-{source}")


def _as_text(value) -> str:
	return str(value or "")


def _as_decimal(value) -> Decimal:
	return Decimal(str(value or 0))


def _delete_by_name(doctype: str, names: set[str]) -> int:
	removed = 0
	for name_batch in _batches(list(names), PAGE_SIZE):
		placeholders = ", ".join(["%s"] * len(name_batch))
		frappe.db.sql(f"DELETE FROM `tab{doctype}` WHERE name IN ({placeholders})", name_batch)
		removed += _row_count()
	return removed


def _delete_temporary_staging_rows(staging_doctype: str) -> int:
	frappe.db.sql(f"DELETE FROM `tab{staging_doctype}` WHERE {_to_rename_sql()} = 1")
	return _row_count()


def _count_temporary_staging_rows(staging_doctype: str) -> int:
	return frappe.db.sql(
		f"SELECT COUNT(*) FROM `tab{staging_doctype}` WHERE {_to_rename_sql()} = 1"
	)[0][0]


def _to_rename_sql() -> str:
	return "CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(json, '$.to_rename')), '0') AS UNSIGNED)"


def _row_count() -> int:
	return frappe.db.sql("SELECT ROW_COUNT()")[0][0]


def _batches(values: list, size: int):
	for start in range(0, len(values), size):
		yield values[start : start + size]
