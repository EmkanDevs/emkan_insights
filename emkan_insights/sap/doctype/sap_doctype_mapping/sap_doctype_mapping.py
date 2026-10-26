# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from emkan_insights.sap.services.mapper import parse_mapping
from emkan_insights.sap.services.exclusions import get_excluded_sap_id_prefixes


class SAPDoctypeMapping(Document):
	def validate(self):
		self._validate_sync_mode()
		if cint(getattr(self, "fetch_limit", 0)) < 0:
			raise frappe.ValidationError("Fetch Limit cannot be negative.")
		if getattr(self, "raw_payload", False) and not getattr(self, "fetch_only", False):
			raise frappe.ValidationError("Raw SAP payloads must use Fetch Only.")
		self.excluded_sap_id_prefixes = ",".join(get_excluded_sap_id_prefixes(self))
		self._validate_external_doctype()
		if getattr(self, "raw_payload", False):
			return

		mapping = parse_mapping(self.field_mapping)
		meta = frappe.get_meta(self.erp_doctype)
		self._validate_sap_id_field(meta)
		self._validate_target_fields(meta, mapping)
		self.field_mapping = json.dumps(mapping, indent=2)

	def _validate_sync_mode(self):
		if not self.full_snapshot_sync and not (
			self.creation_date_filter_field or self.date_filter_field
		):
			raise frappe.ValidationError(
				"Incremental sync requires a SAP creation or update date field. "
				"Enable Full Snapshot Sync when neither field is available."
			)

	def _validate_external_doctype(self):
		meta = frappe.get_meta(self.external_doctype)
		required_fields = {
			"remote_id",
			"source_site",
			"external_sap_configuration",
			"last_synced_on",
			"json",
		}
		missing_fields = sorted(fieldname for fieldname in required_fields if not meta.has_field(fieldname))
		if missing_fields:
			raise frappe.ValidationError(
				f"External DocType '{self.external_doctype}' is missing: {', '.join(missing_fields)}."
			)

	def _validate_sap_id_field(self, meta):
		if not self.erp_sap_id_fieldname:
			return
		field = meta.get_field(self.erp_sap_id_fieldname)
		if not field:
			raise frappe.ValidationError(
				f"ERPNext SAP ID field '{self.erp_sap_id_fieldname}' does not exist on {self.erp_doctype}."
			)
		if field.fieldtype in {"Table", "Table MultiSelect"}:
			raise frappe.ValidationError("ERPNext SAP ID field cannot be a child table.")

	def _validate_target_fields(self, meta, mapping):
		for target_field, rule in mapping.items():
			if target_field == "docstatus":
				if not meta.is_submittable:
					raise frappe.ValidationError(f"ERPNext DocType '{meta.name}' is not submittable.")
				if isinstance(rule, dict) and ("mapping" in rule or "rows" in rule):
					raise frappe.ValidationError("ERPNext docstatus cannot use a child mapping.")
				continue
			field = meta.get_field(target_field)
			if not field:
				raise frappe.ValidationError(
					f"ERPNext field '{target_field}' does not exist on {meta.name}."
				)

			is_child_mapping = isinstance(rule, dict) and ("mapping" in rule or "rows" in rule)
			is_table_field = field.fieldtype in {"Table", "Table MultiSelect"}
			if is_child_mapping != is_table_field:
				raise frappe.ValidationError(
					f"ERPNext field '{target_field}' must use a child mapping only when it is a table field."
				)

			if is_child_mapping:
				child_meta = frappe.get_meta(field.options)
				for child_mapping in rule["rows"] if "rows" in rule else [rule["mapping"]]:
					self._validate_target_fields(child_meta, parse_mapping(child_mapping))
