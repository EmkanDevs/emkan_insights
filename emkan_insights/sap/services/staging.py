from __future__ import annotations

import secrets
from time import time_ns
from typing import Any, Literal

import frappe


SYNC_MAPPING_ERRORS_KEY = "_sap_mapping_errors"


class StagingWriter:
	def __init__(self, external_doctype: str, sap_configuration: str):
		self.external_doctype = external_doctype
		self.sap_configuration = sap_configuration
		self._existing: dict[str, tuple[str, str]] | None = None
		# New rows wait here until flush() inserts them in one statement.
		self._pending: dict[str, list[str]] = {}

	def prefetch(self, remote_ids: list[str]) -> None:
		"""Load existing staging rows for a batch in one query instead of one per record."""
		rows = frappe.get_all(
			self.external_doctype,
			filters={
				"external_sap_configuration": self.sap_configuration,
				"remote_id": ["in", [str(remote_id) for remote_id in remote_ids]],
			},
			fields=["name", "remote_id", "json"],
			limit_page_length=0,
		)
		self._existing = {row.remote_id: (row.name, row.json) for row in rows}
		for remote_id in remote_ids:
			self._existing.setdefault(str(remote_id), None)

	def upsert(
		self, sap_id: Any, payload: dict[str, Any]
	) -> tuple[Literal["created", "updated", "unchanged"], str]:
		remote_id = str(sap_id)
		serialized_payload = frappe.as_json(payload)
		if self._existing is not None and remote_id in self._existing:
			return self._upsert_prefetched(remote_id, payload, serialized_payload)
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

	def _upsert_prefetched(
		self, remote_id: str, payload: dict[str, Any], serialized_payload: str
	) -> tuple[Literal["created", "updated", "unchanged"], str]:
		existing = self._existing[remote_id]
		if existing:
			name, current_json = existing
			if frappe.parse_json(current_json) == payload:
				return "unchanged", name
			if remote_id in self._pending:
				# Created earlier in this batch and not inserted yet: replace the waiting copy.
				self._pending[remote_id][1] = serialized_payload
			else:
				# A direct update skips document hooks and Version rows; `modified` marks the row pending.
				frappe.db.set_value(self.external_doctype, name, "json", serialized_payload)
			self._existing[remote_id] = (name, serialized_payload)
			return "updated", name

		name = self._new_name()
		self._pending[remote_id] = [name, serialized_payload]
		self._existing[remote_id] = (name, serialized_payload)
		return "created", name

	def flush(self) -> None:
		"""Insert the waiting new rows with one statement per chunk.

		Staging rows hold plain data, so this skips the per-document hooks and Version rows
		that cost about a dozen queries per row. Names grow with time, so inserts append to
		the end of the primary key instead of landing on random pages of a large table.
		"""
		if not self._pending:
			return
		now = frappe.utils.now()
		user = frappe.session.user
		fields = [
			"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx",
			"remote_id", "source_site", "external_sap_configuration", "last_synced_on", "json",
		]
		values = [
			(name, now, now, user, user, 0, 0, remote_id, None, self.sap_configuration, None, serialized)
			for remote_id, (name, serialized) in self._pending.items()
		]
		self._pending = {}
		frappe.db.bulk_insert(self.external_doctype, fields, values, chunk_size=500)

	@staticmethod
	def _new_name() -> str:
		return f"{time_ns():x}{secrets.token_hex(2)}"
