"""Allow SAP journal IDs to repeat in different ERPNext companies."""

import frappe


def execute():
	duplicates = frappe.db.sql(
		"""
		SELECT company, custom_sap_jdt_num
		FROM `tabJournal Entry`
		WHERE custom_sap_jdt_num IS NOT NULL AND custom_sap_jdt_num != ''
		GROUP BY company, custom_sap_jdt_num
		HAVING COUNT(*) > 1
		LIMIT 1
		"""
	)
	if duplicates:
		frappe.throw(f"Duplicate SAP journal ID in company: {duplicates[0]}")

	field = "Journal Entry-custom_sap_jdt_num"
	if frappe.db.get_value("Custom Field", field, "unique"):
		frappe.db.set_value("Custom Field", field, "unique", 0)
		frappe.clear_cache(doctype="Journal Entry")
		frappe.db.updatedb("Journal Entry")

	frappe.db.add_unique(
		"Journal Entry",
		["company", "custom_sap_jdt_num"],
		constraint_name="unique_company_sap_journal_id",
	)
