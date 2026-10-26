import frappe


def execute():
	if not frappe.db.table_exists("SAP Doctype Mapping"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabSAP Doctype Mapping`
		SET full_snapshot_sync = CASE
			WHEN TRIM(COALESCE(date_filter_field, '')) = '' THEN 1
			ELSE 0
		END
		"""
	)
