"""Create configured company cost centers when SAP has no matching leaf."""

import frappe
from frappe.utils import now_datetime
from frappe.utils.nestedset import rebuild_tree


def execute():
	configurations = frappe.get_all(
		"External SAP Configuration", fields=["name", "company"], limit_page_length=0
	)
	for company in {row.company for row in configurations if row.company}:
		default_name = frappe.db.get_value("Company", company, "cost_center")
		if not default_name or frappe.db.exists("Cost Center", default_name):
			continue
		now = now_datetime()
		frappe.db.bulk_insert(
			"Cost Center",
			fields=[
				"name", "cost_center_name", "company", "is_group",
				"creation", "modified", "owner", "modified_by",
			],
			values=[(default_name, default_name, company, 0, now, now, "Administrator", "Administrator")],
		)
		rebuild_tree("Cost Center", "parent_cost_center")
	for configuration in configurations:
		default_name = frappe.db.get_value("Company", configuration.company, "cost_center")
		if default_name and frappe.db.get_value("Cost Center", default_name, "is_group") == 0:
			_repair_staged_gl_cost_centers(configuration.name, configuration.company, default_name)


def _repair_staged_gl_cost_centers(configuration: str, company: str, default_name: str) -> None:
	if not frappe.db.table_exists("External GL Entry"):
		return
	now = now_datetime()
	frappe.db.sql(
		"""
		UPDATE `tabExternal GL Entry`
		SET json = JSON_SET(JSON_REMOVE(json, '$._sap_mapping_errors'), '$.cost_center', %s),
			modified = %s
		WHERE external_sap_configuration = %s
			AND JSON_LENGTH(JSON_EXTRACT(json, '$._sap_mapping_errors')) = 1
			AND JSON_UNQUOTE(JSON_EXTRACT(json, '$._sap_mapping_errors[0].field')) = 'cost_center'
			AND JSON_UNQUOTE(JSON_EXTRACT(json, '$._sap_mapping_errors[0].message')) =
				CONCAT('Company ', %s, ' default Cost Center is not a valid leaf.')
		""",
		(default_name, now, configuration, company),
	)
	frappe.db.sql(
		"""
		UPDATE `tabExternal GL Entry`
		SET modified = %s
		WHERE external_sap_configuration = %s
			AND last_synced_on IS NULL
			AND modified > %s
			AND JSON_UNQUOTE(JSON_EXTRACT(json, '$.cost_center')) = %s
			AND JSON_EXTRACT(json, '$._sap_mapping_errors') IS NULL
		""",
		(now, configuration, now, default_name),
	)
