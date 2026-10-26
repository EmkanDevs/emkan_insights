from __future__ import annotations

from typing import Any, Literal

import frappe


class StagingWriter:
	def __init__(self, external_doctype: str, sap_configuration: str):
		self.external_doctype = external_doctype
		self.sap_configuration = sap_configuration

	def upsert(
		self, sap_id: Any, payload: dict[str, Any], sync_hold_reason: str | None = None
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
		if self.external_doctype in {
			"External Sales Invoice", "External Purchase Invoice", "External Payment Entry", "External Account"
		}:
			values["sap_sync_hold_reason"] = sync_hold_reason

		if existing_name:
			doc = frappe.get_doc(self.external_doctype, existing_name)
			if frappe.parse_json(doc.json) == payload and (
				self.external_doctype not in {
					"External Sales Invoice", "External Purchase Invoice", "External Payment Entry", "External Account"
				}
				or doc.sap_sync_hold_reason == sync_hold_reason
			):
				return "unchanged", doc.name
			doc.update(values)
			doc.save(ignore_permissions=True)
			return "updated", doc.name

		doc = frappe.get_doc(
			{"doctype": self.external_doctype, "last_synced_on": None, **values}
		)
		doc.insert(ignore_permissions=True)
		return "created", doc.name
