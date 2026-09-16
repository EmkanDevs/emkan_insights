# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

class ExternalSiteConfiguration(Document):
	pass


@frappe.whitelist(methods=["POST"])
def fetch_and_sync_all_companies() -> dict:
	frappe.has_permission("External Site Configuration", "write", throw=True)

	from emkan_insights.emkan_insights.scheduler.external_site_configuration_scheduler import (
		enqueue_external_site_configuration_scheduler,
	)

	return enqueue_external_site_configuration_scheduler()


FETCH_ONLY_DOCTYPE_MAPPINGS = {
	"Blanket Order": "External Blanket Order",
	"GL Entry": "External GL Entry",
	"Payment Ledger Entry": "External Payment Ledger Entry",
	"Period Closing Voucher": "External Period Closing Voucher",
	"Stock Ledger Entry": "External Stock Ledger Entry",
}


def load_doctypes_before_insert(doc, method=None):
	"""Add template rows that are missing from a new site configuration."""
	fetch_rows = frappe.get_all(
		"Site Configuration Doctypes CT",
		filters={
			"parent": "Site Configuration Settings",
			"parentfield": "fetch_data",
			"parenttype": "Site Configuration Settings",
		},
		fields=["ref_doctype", "exported_doctype", "abbr_required"],
		order_by="idx asc",
		limit_page_length=0,
	)
	existing_ref_doctypes = {row.ref_doctype for row in doc.fetch_data or []}

	for row in fetch_rows:
		if row.ref_doctype in existing_ref_doctypes:
			continue
		doc.append(
			"fetch_data",
			{
				"ref_doctype": row.ref_doctype,
				"exported_doctype": row.exported_doctype,
				"abbr_required": row.abbr_required,
			},
		)
		existing_ref_doctypes.add(row.ref_doctype)


def _get_company_abbr_for_sync(company):
	if not company:
		frappe.throw(_("Company is required for this sync."))

	abbr = frappe.db.get_value("Company", company, "abbr")
	if not abbr:
		frappe.throw(_("Company {0} has no abbreviation set.").format(company))
	return abbr


def _build_company_prefixed_remote_id(company_abbr, remote_id):
	remote_id = str(remote_id or "").strip()
	if not remote_id or remote_id.startswith(f"{company_abbr}-"):
		return remote_id
	return f"{company_abbr}-{remote_id}"
