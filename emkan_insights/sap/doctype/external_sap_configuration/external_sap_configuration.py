# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from emkan_insights.sap.services.client import SAPB1Client
from emkan_insights.sap.mapping_order import mapping_sort_key


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
		mappings = frappe.get_all(
			"SAP Doctype Mapping",
			filters={"enabled": 1},
			fields=["name", "erp_doctype", "external_doctype"],
			order_by="name asc",
		)
		if not existing:
			mappings = [
				mapping for _, mapping in sorted(
					enumerate(mappings),
					key=lambda pair: mapping_sort_key(pair[1].name, pair[0]),
				)
			]
		for mapping in mappings:
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
def fetch_configuration(name: str) -> dict:
	configuration = frappe.get_doc("External SAP Configuration", name)
	frappe.has_permission("External SAP Configuration", "write", doc=configuration, throw=True)
	return _queue_configuration_synchronization(configuration, fetch_only=True)


@frappe.whitelist(methods=["POST"])
def fetch_and_sync_configuration(name: str) -> dict:
	configuration = frappe.get_doc("External SAP Configuration", name)
	frappe.has_permission("External SAP Configuration", "write", doc=configuration, throw=True)
	if not _has_syncable_mappings(configuration.name):
		raise frappe.ValidationError(
			"No enabled target-sync mappings are configured. All enabled mappings are fetch only."
		)
	return _queue_configuration_synchronization(configuration, fetch_only=False)


@frappe.whitelist(methods=["POST"])
def fetch_review_configuration(name: str) -> dict:
	configuration = frappe.get_doc("External SAP Configuration", name)
	frappe.has_permission("External SAP Configuration", "write", doc=configuration, throw=True)

	from emkan_insights.sap.tasks import (
		CONFIGURATION_SYNC_TIMEOUT,
		fetch_sap_review_configuration,
		_configuration_synchronization_job_id,
	)

	job = frappe.enqueue(
		fetch_sap_review_configuration,
		queue="long",
		timeout=CONFIGURATION_SYNC_TIMEOUT,
		job_id=f"{_configuration_synchronization_job_id(configuration.name)}_review",
		deduplicate=True,
		configuration_name=configuration.name,
	)
	return {"status": "queued" if job else "already_queued", "company": configuration.company}


@frappe.whitelist(methods=["POST"])
def fetch_mapping(row_name: str) -> dict:
	row = frappe.get_doc("External Site Configuration CT", row_name)
	if row.parenttype != "External SAP Configuration" or row.parentfield != "mappings":
		raise frappe.ValidationError("The selected row does not belong to an External SAP Configuration.")
	frappe.has_permission("External SAP Configuration", "write", doc=row.parent, throw=True)

	from emkan_insights.sap.tasks import FETCH_LOCK_TTL, _job_id, synchronize_sap_data

	job = frappe.enqueue(
		synchronize_sap_data,
		queue="long",
		timeout=FETCH_LOCK_TTL,
		job_id=_job_id(row.name),
		deduplicate=True,
		row_name=row.name,
		fetch_only=True,
	)
	return {
		"status": "queued" if job else "already_queued",
		"mapping": row.sap_mapping,
	}


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
		job_id=f"sap_manual_sync_{row.name}",
		deduplicate=True,
		row_name=row.name,
	)
	return {
		"status": "queued" if job else "already_queued",
		"mapping": row.sap_mapping,
	}


def _queue_configuration_synchronization(configuration, *, fetch_only: bool) -> dict:
	from emkan_insights.sap.tasks import (
		CONFIGURATION_SYNC_TIMEOUT,
		_configuration_synchronization_job_id,
		synchronize_sap_configuration,
	)

	job = frappe.enqueue(
		synchronize_sap_configuration,
		queue="long",
		timeout=CONFIGURATION_SYNC_TIMEOUT,
		job_id=_configuration_synchronization_job_id(configuration.name),
		deduplicate=True,
		configuration_name=configuration.name,
		fetch_only=fetch_only,
	)
	return {"status": "queued" if job else "already_queued", "company": configuration.company}


def _has_syncable_mappings(configuration_name: str) -> bool:
	mapping_names = frappe.get_all(
		"External Site Configuration CT",
		filters={
			"parent": configuration_name,
			"parenttype": "External SAP Configuration",
			"parentfield": "mappings",
			"sap_mapping": ["is", "set"],
		},
		pluck="sap_mapping",
	)
	if not mapping_names:
		return False
	return bool(frappe.db.exists(
		"SAP Doctype Mapping",
		{"name": ["in", mapping_names], "enabled": 1, "fetch_only": 0},
	))
