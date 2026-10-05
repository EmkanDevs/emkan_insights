import frappe
from frappe.query_builder.functions import Count


CPC_CONFIGURATION = "CPC"


def execute():
	updated = backfill_cpc_source_site()
	frappe.logger("external_sync").info("CPC source_site backfill complete: %s", updated)


def backfill_cpc_source_site() -> dict[str, int]:
	if not frappe.db.exists("External Site Configuration", CPC_CONFIGURATION):
		return {}

	cpc_url = frappe.db.get_value(
		"External Site Configuration",
		CPC_CONFIGURATION,
		"site_url",
	) or ""
	is_cpc_only_site = _get_configuration_names() == {CPC_CONFIGURATION}
	updated = {}

	for doctype in _get_external_doctypes():
		columns = set(frappe.db.get_table_columns(doctype))
		if "source_site" not in columns:
			continue

		table = frappe.qb.DocType(doctype)
		condition = _cpc_source_site_condition(
			table,
			columns,
			cpc_url,
			is_cpc_only_site,
		)
		if condition is None:
			continue

		count = _count_rows(table, condition)
		if not count:
			continue

		frappe.qb.update(table).set(table.source_site, CPC_CONFIGURATION).where(condition).run()
		updated[doctype] = count

	return updated


def get_source_site_issues() -> list[dict]:
	configuration_names = _get_configuration_names()
	issues = []

	for doctype in _get_external_doctypes():
		columns = set(frappe.db.get_table_columns(doctype))
		if "source_site" not in columns:
			issues.append({"doctype": doctype, "issue": "missing_column", "count": None})
			continue

		table = frappe.qb.DocType(doctype)
		blank_count = _count_rows(table, _blank_source_site_condition(table))
		if blank_count:
			issues.append({"doctype": doctype, "issue": "blank", "count": blank_count})

		invalid_condition = (
			~_blank_source_site_condition(table)
			& table.source_site.notin(tuple(configuration_names))
		)
		invalid_count = _count_rows(table, invalid_condition)
		if invalid_count:
			issues.append({
				"doctype": doctype,
				"issue": "unknown_configuration",
				"count": invalid_count,
			})

	return issues


def get_cpc_source_site_candidates() -> dict[str, int]:
	if not frappe.db.exists("External Site Configuration", CPC_CONFIGURATION):
		return {}

	cpc_url = frappe.db.get_value(
		"External Site Configuration",
		CPC_CONFIGURATION,
		"site_url",
	) or ""
	is_cpc_only_site = _get_configuration_names() == {CPC_CONFIGURATION}
	candidates = {}

	for doctype in _get_external_doctypes():
		columns = set(frappe.db.get_table_columns(doctype))
		if "source_site" not in columns:
			continue
		table = frappe.qb.DocType(doctype)
		condition = _cpc_source_site_condition(table, columns, cpc_url, is_cpc_only_site)
		if condition is None:
			continue
		count = _count_rows(table, condition)
		if count:
			candidates[doctype] = count

	return candidates


def _get_external_doctypes() -> list[str]:
	doctypes = frappe.get_all(
		"DocType",
		filters={"name": ["like", "External %"], "istable": 0},
		pluck="name",
		order_by="name",
		limit_page_length=0,
	)
	result = []
	for doctype in doctypes:
		meta = frappe.get_meta(doctype)
		if meta.has_field("source_site") or (meta.has_field("remote_id") and meta.has_field("json")):
			result.append(doctype)
	return result


def _get_configuration_names() -> set[str]:
	return set(frappe.get_all("External Site Configuration", pluck="name", limit_page_length=0))


def _blank_source_site_condition(table):
	return table.source_site.isnull() | (table.source_site == "")


def _cpc_source_site_condition(
	table,
	columns: set[str],
	cpc_url: str,
	is_cpc_only_site: bool,
):
	clean_url = cpc_url.rstrip("/")
	blank_source = _blank_source_site_condition(table)
	if not clean_url:
		return blank_source if is_cpc_only_site else None

	cpc_urls = (clean_url, f"{clean_url}/")
	source_is_cpc_url = table.source_site.isin(cpc_urls)
	candidate = blank_source | source_is_cpc_url
	if is_cpc_only_site:
		return candidate

	attributed_to_cpc = source_is_cpc_url
	if "site_url" in columns:
		attributed_to_cpc |= table.site_url.isin(cpc_urls)
	return candidate & attributed_to_cpc


def _count_rows(table, condition) -> int:
	result = frappe.qb.from_(table).select(Count(table.name)).where(condition).run()
	return result[0][0] if result else 0
