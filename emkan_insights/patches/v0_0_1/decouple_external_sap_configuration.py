import frappe


SAP_CONFIGURATION = "External SAP Configuration"


def execute():
	_backfill_company()
	_clear_sap_source_sites()
	_update_existing_entity_mappings()


def _backfill_company():
	columns = set(frappe.db.get_table_columns(SAP_CONFIGURATION))
	if "external_site_configuration" not in columns:
		return

	configurations = frappe.db.sql(
		"""
		select name, external_site_configuration
		from `tabExternal SAP Configuration`
		where ifnull(company, '') = ''
		  and ifnull(external_site_configuration, '') != ''
		""",
		as_dict=True,
	)
	for configuration in configurations:
		company = frappe.db.get_value(
			"External Site Configuration",
			configuration.external_site_configuration,
			"company",
		)
		if company:
			frappe.db.set_value(
				SAP_CONFIGURATION,
				configuration.name,
				"company",
				company,
				update_modified=False,
			)


def _clear_sap_source_sites():
	for doctype in _external_staging_doctypes():
		columns = set(frappe.db.get_table_columns(doctype))
		if not {"source_site", "external_sap_configuration"}.issubset(columns):
			continue

		frappe.db.set_value(
			doctype,
			{"external_sap_configuration": ["is", "set"]},
			"source_site",
			None,
			update_modified=False,
		)


def _update_existing_entity_mappings():
	for row in frappe.get_all(
		"Global Entity Mapping Item",
		fields=["name", "entity_type", "entity_name"],
	):
		if not row.entity_type or not row.entity_name:
			continue

		meta = frappe.get_meta(row.entity_type)
		if not meta.has_field("external_sap_configuration"):
			continue

		sap_configuration = frappe.db.get_value(
			row.entity_type,
			row.entity_name,
			"external_sap_configuration",
		)
		if sap_configuration:
			frappe.db.set_value(
				"Global Entity Mapping Item",
				row.name,
				"source_site",
				sap_configuration,
				update_modified=False,
			)


def _external_staging_doctypes():
	return frappe.get_all(
		"DocType",
		filters={
			"module": "Emkan Insights",
			"name": ["like", "External %"],
			"istable": 0,
		},
		pluck="name",
	)
