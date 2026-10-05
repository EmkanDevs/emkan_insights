import frappe


def execute():
	for name in frappe.get_all("External SAP Configuration", pluck="name"):
		configuration = frappe.get_doc("External SAP Configuration", name)
		configuration._add_missing_mappings()
		configuration.save(ignore_permissions=True)

	old_child_doctype = "External SAP Configuration CT"
	if frappe.db.exists("DocType", old_child_doctype):
		frappe.delete_doc("DocType", old_child_doctype, force=True, ignore_permissions=True)
