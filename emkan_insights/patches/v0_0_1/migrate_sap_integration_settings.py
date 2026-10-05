import frappe
from frappe.utils.password import get_decrypted_password


OLD_DOCTYPE = "SAP Integration Settings"
NEW_DOCTYPE = "External SAP Configuration"


def execute():
	if not frappe.db.exists("DocType", OLD_DOCTYPE):
		return

	settings = frappe._dict(frappe.db.get_singles_dict(OLD_DOCTYPE))
	settings.password = get_decrypted_password(OLD_DOCTYPE, OLD_DOCTYPE, "password")
	configuration = _get_or_create_configuration(settings)
	_backfill_staging_links(configuration, settings.source_site)
	frappe.delete_doc("DocType", OLD_DOCTYPE, force=True, ignore_permissions=True)


def _get_or_create_configuration(settings):
	name = settings.source_site
	if frappe.db.exists(NEW_DOCTYPE, name):
		return frappe.get_doc(NEW_DOCTYPE, name)

	return frappe.get_doc(
		{
			"doctype": NEW_DOCTYPE,
			"configuration_name": name,
			"company": frappe.db.get_value(
				"External Site Configuration", settings.source_site, "company"
			),
			"base_url": settings.base_url,
			"company_db": settings.company_db,
			"username": settings.username,
			"password": settings.password,
			"session_timeout_minutes": settings.session_timeout_minutes or 25,
		}
	).insert(ignore_permissions=True)


def _backfill_staging_links(configuration, source_site):
	for doctype in frappe.get_all(
		"DocType",
		filters={"module": "Emkan Insights", "name": ["like", "External %"], "istable": 0},
		pluck="name",
	):
		if not frappe.get_meta(doctype).has_field("external_sap_configuration"):
			continue
		frappe.db.set_value(
			doctype,
			{
				"source_site": source_site,
				"external_sap_configuration": ["is", "not set"],
			},
			{
				"source_site": None,
				"external_sap_configuration": configuration.name,
			},
			update_modified=False,
		)
