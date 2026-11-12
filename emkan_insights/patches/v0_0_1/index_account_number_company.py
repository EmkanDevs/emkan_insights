import frappe


def execute():
	"""SAP mapping looks up Accounts by account_number and company on every row."""
	frappe.db.add_index("Account", ["account_number", "company"], index_name="account_number_company")
