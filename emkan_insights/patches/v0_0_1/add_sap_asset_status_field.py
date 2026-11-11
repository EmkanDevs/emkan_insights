"""Preserve the SAP fixed Asset lifecycle state on ERPNext Assets."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields({
		"Asset": [{
			"fieldname": "custom_sap_asset_status",
			"label": "SAP Asset Status",
			"fieldtype": "Select",
			"options": "New\nActive\nInactive",
			"insert_after": "status",
			"read_only": 1,
			"no_copy": 1,
		}],
	})
