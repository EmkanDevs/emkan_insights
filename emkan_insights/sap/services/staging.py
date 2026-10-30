from __future__ import annotations

from typing import Any, Literal

import frappe


SYNC_MAPPING_ERRORS_KEY = "_sap_mapping_errors"


class StagingWriter:
	def __init__(self, external_doctype: str, sap_configuration: str):
		self.external_doctype = external_doctype
		self.sap_configuration = sap_configuration

	def upsert(
		self, sap_id: Any, payload: dict[str, Any]
	) -> tuple[Literal["created", "updated", "unchanged"], str]:
		remote_id = str(sap_id)
		serialized_payload = frappe.as_json(payload)
		existing_name = frappe.db.get_value(
			self.external_doctype,
			{
				"external_sap_configuration": self.sap_configuration,
				"remote_id": remote_id,
			},
			"name",
		)
		values = {
			"remote_id": remote_id,
			"source_site": None,
			"external_sap_configuration": self.sap_configuration,
			"json": serialized_payload,
		}
		if existing_name:
			doc = frappe.get_doc(self.external_doctype, existing_name)
			if frappe.parse_json(doc.json) == payload:
				return "unchanged", doc.name
			doc.update(values)
			doc.save(ignore_permissions=True)
			return "updated", doc.name

		doc = frappe.get_doc(
			{"doctype": self.external_doctype, "last_synced_on": None, **values}
		)
		doc.insert(ignore_permissions=True)
		return "created", doc.name
