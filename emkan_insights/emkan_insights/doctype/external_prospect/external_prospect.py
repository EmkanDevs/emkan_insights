# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ExternalProspect(Document):
	pass



import frappe
import json
from frappe.utils import cint, flt
from frappe.model.naming import set_new_name


SYSTEM_FIELDS = {
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "doctype", "__last_sync_on",
	"parent", "parentfield", "parenttype"
}

NON_DATA_FIELDS = {
	"naming_series",
	"address_html", "contact_html", "notes_html",
	"open_activities_html", "all_activities_html",
	"column_break0", "column_break1", "column_break3",
	"column_break_10", "column_break_17", "column_break_22",
	"column_break_23", "column_break_31", "column_break_33",
	"column_break_36", "column_break_54", "column_break_56",
	"contact_info", "organization_details_section",
	"address_contact_section", "primary_contact_section",
	"section_break_14", "section_break_32", "more_info",
	"lost_detail_section", "items_section", "all_activities_section",
	"activities_tab", "notes_tab", "dashboard_tab"
}

HANDLED_SEPARATELY = {
	"company", "docstatus", "notes", "items", "title",
	"prospect_name", "prospect_lead",
	"contact_person", "customer_address"
}


@frappe.whitelist()
def sync_external_prospects(names=None):

	if isinstance(names, str):
		names = json.loads(names)

	if not names:
		names = frappe.get_all("External Prospect", pluck="name")

	batch_size = 200

	for i in range(0, len(names), batch_size):
		batch = names[i:i + batch_size]

		frappe.enqueue(
			"emkan_insights.emkan_insights.doctype.external_prospect.external_prospect.sync_prospect_batch",
			queue="long",
			timeout=5000,
			names=batch
		)

	return f"{len(names)} Prospect(s) queued for sync"


def sync_prospect_batch(names):

	success = 0
	failed = 0

	for idx, source_name in enumerate(names, start=1):

		try:
			sync_single_prospect(source_name)
			success += 1

			if idx % 20 == 0:
				frappe.db.commit()

		except Exception:

			failed += 1

			frappe.log_error(
				title=f"External Prospect Sync Failed: {source_name}",
				message=frappe.get_traceback()
			)

	frappe.db.commit()

	return {
		"success": success,
		"failed": failed
	}


def get_default_company():
	company = frappe.defaults.get_user_default("Company")
	if not company:
		company = frappe.defaults.get_global_default("company")
	if not company:
		company = frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		company = frappe.get_cached_value("Company", {}, "name")
	return company


def _get_company_abbr(company):
	abbr = frappe.db.get_value("Company", company, "abbr")
	if not abbr:
		frappe.throw(f"Company '{company}' has no abbreviation set")
	return abbr


def _build_target_name(company_abbr, base_id):
	base_id = (base_id or "").strip()
	if not base_id:
		return None

	if base_id.startswith(f"{company_abbr}-"):
		return base_id

	return f"{company_abbr}-{base_id}"


def resolve_local_link(doctype, remote_value, company_abbr):
	if not remote_value:
		return None

	if frappe.get_meta(doctype).has_field("remote_id"):
		local_name = frappe.db.get_value(
			doctype, {"remote_id": remote_value}, "name"
		)
		if local_name:
			return local_name

	if frappe.db.exists(doctype, remote_value):
		return remote_value

	if company_abbr and not remote_value.startswith(f"{company_abbr}-"):
		prefixed = f"{company_abbr}-{remote_value}"
		if frappe.db.exists(doctype, prefixed):
			return prefixed

	return None


def sync_single_prospect(source_name):

	src = frappe.get_doc("External Prospect", source_name)

	company = getattr(src, "company", None) or get_default_company()
	company_abbr = _get_company_abbr(company)

	# Use company_name for the Prospect ID, fallback to source_name
	company_name = getattr(src, "company_name", None) or source_name
	# Clean for ID: replace spaces with hyphens, remove dots
	clean_name = company_name.strip().replace(" ", "-").replace(".", "")
	
	target_name = _build_target_name(company_abbr, clean_name)

	existing = None
	if target_name and frappe.db.exists("Prospect", target_name):
		existing = target_name

	if existing:
		prospect = frappe.get_doc("Prospect", existing)
	else:
		prospect = frappe.new_doc("Prospect")
		# Set name with company prefix
		prospect.name = target_name or f"{company_abbr}-{clean_name}"
		prospect.flags.name_set = True

	prospect_meta = frappe.get_meta("Prospect")
	valid_columns = set(prospect_meta.get_valid_columns())

	src_data = src.as_dict()

	for fieldname, value in src_data.items():
		if fieldname in SYSTEM_FIELDS:
			continue
		if fieldname in NON_DATA_FIELDS:
			continue
		if fieldname in HANDLED_SEPARATELY:
			continue
		if fieldname not in valid_columns:
			continue
		prospect.set(fieldname, value)

	prospect.company = company
	prospect.docstatus = cint(getattr(src, "docstatus", 0))

	prospect.prospect_name = (
		getattr(src, "company_name", None)
		or getattr(src, "prospect_name", None)
		or getattr(src, "title", None)
		or src.name
	)
	prospect.title = prospect.prospect_name

	status = getattr(src, "status", None)
	if status:
		prospect.status = status

	remote_prospect_lead = getattr(src, "prospect_lead", None)
	if remote_prospect_lead:
		resolved_lead = resolve_local_link(
			"Lead", remote_prospect_lead, company_abbr
		)
		prospect.prospect_lead = resolved_lead
		if not resolved_lead:
			frappe.log_error(
				title=f"Prospect Sync - lead not resolved: {source_name}",
				message=(
					f"remote prospect_lead={remote_prospect_lead}, "
					f"company_abbr={company_abbr}. Leaving unset."
				)
			)

	remote_contact_person = getattr(src, "contact_person", None)
	if remote_contact_person:
		resolved_contact = resolve_local_link(
			"Contact", remote_contact_person, company_abbr
		)
		prospect.contact_person = resolved_contact
		if not resolved_contact:
			frappe.log_error(
				title=f"Prospect Sync - contact not resolved: {source_name}",
				message=(
					f"remote contact_person={remote_contact_person}, "
					f"company_abbr={company_abbr}. Leaving unset."
				)
			)

	remote_customer_address = getattr(src, "customer_address", None)
	if remote_customer_address:
		resolved_address = resolve_local_link(
			"Address", remote_customer_address, company_abbr
		)
		prospect.customer_address = resolved_address
		if not resolved_address:
			frappe.log_error(
				title=f"Prospect Sync - address not resolved: {source_name}",
				message=(
					f"remote customer_address={remote_customer_address}, "
					f"company_abbr={company_abbr}. Leaving unset."
				)
			)

	def sync_child_table(target_field, source_field, child_doctype):
		prospect.set(target_field, [])
		if not hasattr(src, source_field):
			return

		child_meta = frappe.get_meta(child_doctype)
		child_valid_columns = set(child_meta.get_valid_columns())

		rows = getattr(src, source_field, []) or []
		for row in rows:
			row_data = row.as_dict()
			new_row = {}
			for fieldname, value in row_data.items():
				if fieldname in SYSTEM_FIELDS:
					continue
				if fieldname not in child_valid_columns:
					continue
				if value is None:
					continue
				new_row[fieldname] = value
			if new_row:
				prospect.append(target_field, new_row)

	sync_child_table("notes", "notes", "CRM Note")

	prospect.flags.ignore_permissions = True
	prospect.flags.ignore_mandatory = True
	prospect.flags.ignore_validate = True
	prospect.flags.ignore_links = True

	if existing:
		prospect.db_update()
		prospect.update_children()
	else:
		if not prospect.name:
			prospect.name = f"{company_abbr}-{clean_name}"
		prospect.db_insert()
		for df in prospect.meta.get_table_fields():
			for d in prospect.get(df.fieldname):
				d.db_insert()

	return prospect.name