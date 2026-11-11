"""Track SAP acquisition amounts used to value imported draft Assets."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields({
		"Asset": [
			{
				"fieldname": "custom_sap_acquisition_amount",
				"label": "SAP Acquisition Amount",
				"fieldtype": "Currency",
				"options": "Company:company:default_currency",
				"insert_after": "gross_purchase_amount",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_sap_acquisition_sources",
				"label": "SAP Acquisition Documents",
				"fieldtype": "Small Text",
				"insert_after": "custom_sap_acquisition_amount",
				"read_only": 1,
				"no_copy": 1,
			},
		],
	})
