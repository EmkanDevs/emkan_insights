import frappe


def execute():
	"""GL Entry sync resolves each journal by custom_sap_jdt_num; without an index that scans Journal Entry."""
	if frappe.db.has_column("Journal Entry", "custom_sap_jdt_num"):
		frappe.db.add_index("Journal Entry", ["custom_sap_jdt_num", "company"], index_name="sap_jdt_num_company")
