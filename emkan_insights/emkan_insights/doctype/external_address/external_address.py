# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document


class ExternalAddress(Document):
	pass


SYSTEM_FIELDS = {
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "doctype", "__last_sync_on",
	"parent", "parentfield", "parenttype"
}


def _dedupe_address_children(parent_name, parenttype):
	"""
	Remove duplicate Dynamic Link rows from Address / External Address.
	Same pattern as _dedupe_purchase_invoice_children in external_pi_sync.
	Keeps the earliest row per (link_doctype, link_name).
	"""

	def _dedupe_table(doctype, parentfield, key_fields):
		if not frappe.db.exists("DocType", doctype):
			return

		rows = frappe.db.get_all(
			doctype,
			filters={
				"parent": parent_name,
				"parenttype": parenttype,
				"parentfield": parentfield,
			},
			fields=["name"] + key_fields,
			order_by="creation asc",
		)

		seen = set()
		delete_rows = []

		for row in rows:
			key = tuple(
				str(row.get(field) or "").strip().lower()
				for field in key_fields
			)

			if key in seen:
				delete_rows.append(row.name)
			else:
				seen.add(key)

		if delete_rows:
			frappe.db.delete(
				doctype,
				{"name": ["in", delete_rows]}
			)

	_dedupe_table(
		"Dynamic Link",
		"links",
		[
			"link_doctype",
			"link_name",
		],
	)

	frappe.db.commit()


def _safe_dedupe(parent_name, parenttype):
	"""
	Wrapper so a bug inside dedupe can never bubble up and mark an
	otherwise-successful sync as failed (same as PI _safe_dedupe).
	"""
	try:
		_dedupe_address_children(parent_name, parenttype)
	except Exception:
		frappe.log_error(
			title=f"Address dedupe failed for {parenttype} {parent_name}",
			message=frappe.get_traceback()
		)


@frappe.whitelist()
def sync_external_records(names):
	if isinstance(names, str):
		names = json.loads(names)

	results = []

	for name in names:
		try:
			src = frappe.get_doc("External Address", name)

			# Clean duplicates on source first (PI-style DB dedupe)
			_safe_dedupe(src.name, "External Address")
			frappe.clear_document_cache("External Address", src.name)
			src = frappe.get_doc("External Address", name)

			# Check if Address already exists
			existing = frappe.db.get_value("Address", {"name": src.name}, "name")

			if existing:
				doc = frappe.get_doc("Address", existing)
			else:
				doc = frappe.new_doc("Address")
				doc.name = src.name
				doc.flags.name_set = True

			# Copy fields
			for field, value in src.as_dict().items():
				if field not in SYSTEM_FIELDS and hasattr(doc, field):
					if isinstance(value, list):
						# Clear existing child rows from DB for update path
						if existing and field in [df.fieldname for df in doc.meta.get_table_fields()]:
							child_table = doc.meta.get_field(field).options
							frappe.db.delete(
								child_table,
								{
									"parent": doc.name,
									"parenttype": "Address",
									"parentfield": field,
								},
							)

						doc.set(field, [])

						for row in value:
							if isinstance(row, dict):
								clean_row = {
									k: v for k, v in row.items() if k not in SYSTEM_FIELDS
								}
								doc.append(field, clean_row)
					else:
						doc.set(field, value)

			# Ensure remote_id is set
			if hasattr(doc, "remote_id"):
				doc.remote_id = src.name

			# Flags
			doc.flags.ignore_permissions = True
			doc.flags.ignore_mandatory = True
			doc.flags.ignore_validate = True
			doc.flags.ignore_links = True

			if existing:
				doc.save(ignore_permissions=True)
			else:
				doc.insert(ignore_permissions=True, ignore_mandatory=True)

			frappe.db.commit()

			# Clean duplicates on target Address after save (same as PI)
			_safe_dedupe(doc.name, "Address")

			results.append({
				"name": name,
				"status": "updated" if existing else "synced",
				"address": doc.name
			})

		except Exception:
			error = frappe.get_traceback()
			frappe.log_error(
				title=f"Address Sync Error: {name}",
				message=error
			)
			results.append({"name": name, "status": "failed"})

	return results
