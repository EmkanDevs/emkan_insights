# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from emkan_insights.sap.services.client import SAPB1Client


class ExternalSAPConfiguration(Document):
	def before_insert(self):
		self._add_missing_mappings()

	def before_save(self):
		self._add_missing_mappings()

	def validate(self):
		if not (self.base_url or "").startswith(("http://", "https://")):
			raise frappe.ValidationError("SAP Base URL must start with http:// or https://.")
		if cint(self.session_timeout_minutes) < 1:
			raise frappe.ValidationError("Session Timeout must be at least one minute.")

	def _add_missing_mappings(self):
		existing = {row.sap_mapping for row in self.mappings or []}
		for mapping in frappe.get_all(
			"SAP Doctype Mapping",
			filters={"enabled": 1},
			fields=["name", "erp_doctype", "external_doctype"],
			order_by="name asc",
		):
			if mapping.name not in existing:
				self.append(
					"mappings",
					{
						"sap_mapping": mapping.name,
						"ref_doctype": mapping.erp_doctype,
						"exported_doctype": mapping.external_doctype,
					},
				)


@frappe.whitelist(methods=["POST"])
def check_connection(name: str) -> dict:
	frappe.has_permission("External SAP Configuration", "write", doc=name, throw=True)
	client = SAPB1Client.from_configuration(name)
	client.login()
	return {"status": "ok", "message": frappe._("Connection is working.")}


@frappe.whitelist(methods=["POST"])
def fetch_mapping(row_name: str) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")
	frappe.has_permission("External SAP Configuration", "write", doc=row.parent, throw=True)

	from emkan_insights.sap.tasks import fetch_sap_data

	return fetch_sap_data(row.name)


@frappe.whitelist(methods=["POST"])
def queue_sync_mapping(row_name: str) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")
	frappe.has_permission("External SAP Configuration", "write", doc=row.parent, throw=True)

	from emkan_insights.sap.tasks import FETCH_LOCK_TTL, sync_sap_data

	job = frappe.enqueue(
		sync_sap_data,
		queue="long",
		timeout=FETCH_LOCK_TTL,
		enqueue_after_commit=True,
		job_id=f"sap_manual_sync_{row.name}",
		deduplicate=True,
		row_name=row.name,
	)
	return {
		"status": "queued" if job else "already_queued",
		"mapping": row.sap_mapping,
	}
