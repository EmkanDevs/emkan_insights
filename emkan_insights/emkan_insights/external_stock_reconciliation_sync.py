import json
from contextlib import contextmanager

import frappe
from frappe.utils import cint


SYSTEM_FIELDS = {
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "doctype", "__last_sync_on",
	"parent", "parentfield", "parenttype",
}

IGNORE_ITEM_FIELDS = {
	"last_scanned_warehouse",
	"add_serial_batch_bundle",
	"has_item_scanned",
}

PARENT_LINKS = {
	"set_warehouse": "Warehouse",
	"expense_account": "Account",
	"cost_center": "Cost Center",
	"amended_from": "Stock Reconciliation",
}

ITEM_LINKS = {
	"item_code": "Item",
	"warehouse": "Warehouse",
	"stock_uom": "UOM",
	"batch_no": "Batch",
	"serial_and_batch_bundle": "Serial and Batch Bundle",
	"current_serial_and_batch_bundle": "Serial and Batch Bundle",
	"item_group": "Item Group",
}


@contextmanager
def savepoint_context(name):
	safe_name = frappe.scrub(name)[:50]
	frappe.db.savepoint(safe_name)
	try:
		yield
	except Exception:
		frappe.db.rollback(save_point=safe_name)
		raise


def _log(title, message):
	short_title = (title[:135] + "...") if len(title) > 135 else title
	frappe.log_error(title=short_title, message=message or "")


def _get_company_abbr(company):
	abbr = frappe.db.get_value("Company", company, "abbr")
	if not abbr:
		frappe.throw(f"Company '{company}' has no abbreviation set")
	return abbr


def _build_target_name(company_abbr, base_id):
	
	base_id = (base_id or "").strip()
	if not base_id:
		frappe.throw("Missing base id for Stock Reconciliation naming")

	if base_id.startswith(f"{company_abbr}-"):
		return base_id

	return f"{company_abbr}-{base_id}"


def _prefix_reference(company_abbr, value):
	value = str(value or "").strip()
	if not value:
		return None
	if value.startswith(f"{company_abbr}-"):
		return value
	return f"{company_abbr}-{value}"


def _doctype_has_field(doctype, fieldname):
	meta = frappe.get_meta(doctype)
	return meta.has_field(fieldname) or fieldname in meta.get_valid_columns()


def _resolve_link_with_prefix(doctype, remote_name, company_abbr=None):
	if not remote_name:
		return None

	remote_name = str(remote_name).strip()
	if not remote_name:
		return None

	if not frappe.db.exists("DocType", doctype):
		return remote_name

	if company_abbr and remote_name.startswith(f"{company_abbr}-"):
		if frappe.db.exists(doctype, remote_name):
			return remote_name

	if company_abbr:
		prefixed = f"{company_abbr}-{remote_name}"
		if frappe.db.exists(doctype, prefixed):
			return prefixed

	if frappe.db.exists(doctype, remote_name):
		return remote_name

	meta = frappe.get_meta(doctype)
	for field in ("remote_id", "custom_remote_id"):
		if meta.has_field(field):
			local_name = frappe.db.get_value(doctype, {field: remote_name}, "name")
			if local_name:
				return local_name
			if company_abbr:
				local_name = frappe.db.get_value(
					doctype, {field: f"{company_abbr}-{remote_name}"}, "name"
				)
				if local_name:
					return local_name

	return None


def _resolve_item_code(item_code, company_abbr=None):
	if not item_code:
		return None

	item_code = str(item_code).strip()
	if frappe.db.exists("Item", item_code):
		return item_code

	resolved = _resolve_link_with_prefix("Item", item_code, company_abbr)
	if resolved:
		return resolved

	mapped = frappe.db.get_value("External Item", item_code, "remote_id")
	if mapped and frappe.db.exists("Item", mapped):
		return mapped

	return item_code


def _set_docstatus_sql(doctype, docname, docstatus, status=None):
	if status is not None and _doctype_has_field(doctype, "status"):
		frappe.db.sql(
			f"""
			UPDATE `tab{doctype}`
			SET docstatus = %s,
				status = %s,
				modified = NOW(),
				modified_by = SUBSTRING_INDEX(USER(), '@', 1)
			WHERE name = %s
			""",
			(docstatus, status, docname),
		)
	else:
		frappe.db.sql(
			f"""
			UPDATE `tab{doctype}`
			SET docstatus = %s,
				modified = NOW(),
				modified_by = SUBSTRING_INDEX(USER(), '@', 1)
			WHERE name = %s
			""",
			(docstatus, docname),
		)

	meta = frappe.get_meta(doctype)
	for df in meta.get_table_fields():
		child_doctype = df.options
		if frappe.db.exists("DocType", child_doctype):
			frappe.db.sql(
				f"""
				UPDATE `tab{child_doctype}`
				SET docstatus = %s,
					modified = NOW(),
					modified_by = SUBSTRING_INDEX(USER(), '@', 1)
				WHERE parent = %s AND parenttype = %s
				""",
				(docstatus, docname, doctype),
			)

	frappe.clear_document_cache(doctype, docname)


def _force_cancel_doc(doctype, docname):
	_set_docstatus_sql(doctype, docname, 2, "Cancelled")


def _force_submit_doc(doctype, docname):
	_set_docstatus_sql(doctype, docname, 1, "Submitted")


def _safe_submit_doc(doc, log_prefix=""):
	try:
		doc.flags.ignore_permissions = True
		doc.submit()
		return True
	except Exception as submit_err:
		current = cint(frappe.db.get_value(doc.doctype, doc.name, "docstatus"))
		if current == 1:
			return True
		_log(f"{log_prefix} normal submit failed, attempting force submit", f"Error: {str(submit_err)}")
		try:
			_force_submit_doc(doc.doctype, doc.name)
			return True
		except Exception as force_submit_err:
			_log(
				f"{log_prefix} force submit also failed",
				f"Normal: {str(submit_err)}\nForce: {str(force_submit_err)}",
			)
			raise


def _safe_cancel_doc(doc, log_prefix=""):
	try:
		doc.flags.ignore_permissions = True
		doc.cancel()
		return True
	except Exception as normal_cancel_err:
		_log(f"{log_prefix} normal cancel failed, attempting force cancel", f"Error: {str(normal_cancel_err)}")
		try:
			_force_cancel_doc(doc.doctype, doc.name)
			return True
		except Exception as force_cancel_err:
			_log(
				f"{log_prefix} force cancel also failed",
				f"Normal: {str(normal_cancel_err)}\nForce: {str(force_cancel_err)}",
			)
			raise


def _sync_existing_docstatus(existing_name, external):
	local_docstatus = cint(frappe.db.get_value("Stock Reconciliation", existing_name, "docstatus"))
	target_docstatus = cint(external.docstatus)

	if local_docstatus == target_docstatus:
		return {"name": existing_name, "status": "exists", "docstatus": local_docstatus}

	try:
		sr = frappe.get_doc("Stock Reconciliation", existing_name)

		if target_docstatus == 2:
			if local_docstatus == 0:
				_safe_submit_doc(sr, log_prefix=f"Stock Reconciliation {existing_name}")
				sr.reload()
			_safe_cancel_doc(sr, log_prefix=f"Stock Reconciliation {existing_name}")
			final = cint(frappe.db.get_value("Stock Reconciliation", existing_name, "docstatus"))
			return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}

		if target_docstatus == 1 and local_docstatus == 0:
			_safe_submit_doc(sr, log_prefix=f"Stock Reconciliation {existing_name}")
			final = cint(frappe.db.get_value("Stock Reconciliation", existing_name, "docstatus"))
			return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}

		return {
			"name": existing_name,
			"status": "exists",
			"warning": "unhandled transition",
			"docstatus": local_docstatus,
		}
	except Exception as e:
		final = cint(frappe.db.get_value("Stock Reconciliation", existing_name, "docstatus"))
		if final == target_docstatus:
			return {"name": existing_name, "status": "docstatus_synced", "docstatus": final}
		_log(f"Stock Reconciliation {existing_name} docstatus transition failed", frappe.get_traceback())
		return {"name": existing_name, "status": "exists", "error": str(e)}


def _build_item_rows(external, company_abbr):
	rows = []
	if not getattr(external, "items", None):
		return rows

	for row in external.items:
		item_row = {}
		for field, value in row.as_dict().items():
			if field in SYSTEM_FIELDS or field in IGNORE_ITEM_FIELDS:
				continue
			item_row[field] = value

		for fieldname, linked_doctype in ITEM_LINKS.items():
			raw = item_row.get(fieldname)
			if not raw:
				continue
			if fieldname == "item_code":
				item_row[fieldname] = _resolve_item_code(raw, company_abbr)
			else:
				resolved = _resolve_link_with_prefix(linked_doctype, raw, company_abbr)
				# Clear missing serial/batch bundles so submit is not blocked
				if fieldname in ("serial_and_batch_bundle", "current_serial_and_batch_bundle"):
					item_row[fieldname] = resolved
				else:
					item_row[fieldname] = resolved or raw

		rows.append(item_row)

	return rows


def sync_external_stock_reconciliation(external_name, company):
	external = frappe.get_doc("External Stock Reconciliation", external_name)

	company_abbr = _get_company_abbr(company)
	source_id = (external.remote_id or external.name or "").strip()
	target_name = _build_target_name(company_abbr, source_id)

	existing = None
	if _doctype_has_field("Stock Reconciliation", "remote_id"):
		existing = frappe.db.get_value("Stock Reconciliation", {"remote_id": source_id}, "name")
	if not existing:
		existing = target_name if frappe.db.exists("Stock Reconciliation", target_name) else None

	if existing:
		return _sync_existing_docstatus(existing, external)

	sr = frappe.new_doc("Stock Reconciliation")
	sr.name = target_name
	sr.flags.name_set = True
	sr.flags.ignore_naming_series = True

	if _doctype_has_field("Stock Reconciliation", "remote_id"):
		sr.remote_id = source_id
	if _doctype_has_field("Stock Reconciliation", "source_site"):
		sr.source_site = getattr(external, "source_site", None)

	sr.company = company
	sr.purpose = external.purpose or "Stock Reconciliation"
	sr.posting_date = external.posting_date
	sr.posting_time = external.posting_time
	sr.set_posting_time = cint(getattr(external, "set_posting_time", 0))
	sr.scan_mode = cint(getattr(external, "scan_mode", 0))
	sr.difference_amount = getattr(external, "difference_amount", None)

	for fieldname, linked_doctype in PARENT_LINKS.items():
		raw = getattr(external, fieldname, None)
		if not raw:
			continue
		if fieldname == "amended_from":
			
			candidate = _build_target_name(company_abbr, raw)
			if frappe.db.exists("Stock Reconciliation", candidate):
				sr.amended_from = candidate
			elif _doctype_has_field("Stock Reconciliation", "remote_id"):
				found = frappe.db.get_value("Stock Reconciliation", {"remote_id": raw}, "name")
				sr.amended_from = found or candidate
			else:
				sr.amended_from = candidate
		else:
			resolved = _resolve_link_with_prefix(linked_doctype, raw, company_abbr)
			setattr(sr, fieldname, resolved or raw)

	item_rows = _build_item_rows(external, company_abbr)
	if not item_rows:
		frappe.throw(f"External Stock Reconciliation {external_name} has no item rows")

	sr.set("items", item_rows)

	sr.flags.ignore_permissions = True
	sr.flags.ignore_link_validation = True
	sr.flags.ignore_naming_series = True
	sr.insert(ignore_permissions=True)

	if sr.name != target_name and not frappe.db.exists("Stock Reconciliation", target_name):
		old_name = sr.name
		frappe.db.sql(
			"UPDATE `tabStock Reconciliation` SET name = %s WHERE name = %s",
			(target_name, old_name),
		)
		frappe.db.sql(
			"""
			UPDATE `tabStock Reconciliation Item`
			SET parent = %s
			WHERE parent = %s AND parenttype = 'Stock Reconciliation'
			""",
			(target_name, old_name),
		)
		sr.name = target_name

	if _doctype_has_field("Stock Reconciliation", "remote_id"):
		frappe.db.set_value(
			"Stock Reconciliation", sr.name, "remote_id", source_id, update_modified=False
		)

	target_docstatus = cint(external.docstatus)
	frappe.clear_document_cache("Stock Reconciliation", sr.name)
	sr = frappe.get_doc("Stock Reconciliation", sr.name)

	if target_docstatus == 1:
		_safe_submit_doc(sr, log_prefix=f"Stock Reconciliation {sr.name}")
	elif target_docstatus == 2:
		_safe_submit_doc(sr, log_prefix=f"Stock Reconciliation {sr.name}")
		sr = frappe.get_doc("Stock Reconciliation", sr.name)
		_safe_cancel_doc(sr, log_prefix=f"Stock Reconciliation {sr.name}")

	final = cint(frappe.db.get_value("Stock Reconciliation", sr.name, "docstatus"))
	return {
		"name": sr.name,
		"status": "synced",
		"docstatus": final,
		"items_count": frappe.db.count(
			"Stock Reconciliation Item",
			{"parent": sr.name, "parenttype": "Stock Reconciliation"},
		),
	}


@frappe.whitelist()
def sync_stock_reconciliation_docs(source_doctype=None, names=None):
	if isinstance(names, str):
		names = json.loads(names)

	if not isinstance(names, (list, tuple)):
		return []

	results = []
	for name in names:
		try:
			with savepoint_context(f"sync_sr_{name}"):
				external_doc = frappe.get_doc(source_doctype or "External Stock Reconciliation", name)
				result = sync_external_stock_reconciliation(name, external_doc.company)
				results.append(result)
		except Exception as e:
			frappe.log_error(
				title=f"Stock Reconciliation Sync Error: {name}",
				message=frappe.get_traceback(),
			)
			results.append({
				"name": name,
				"status": "failed",
				"error": str(e),
			})

	return results
