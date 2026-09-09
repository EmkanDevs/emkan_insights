# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

import json
from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
	FETCH_ONLY_DOCTYPE_MAPPINGS,
	_GENERIC_DOCTYPE_MAP,
	_fetch_generic_docs_threaded,
	_process_item,
	_serialize_remote_payload,
)


class TestExternalSiteConfiguration(FrappeTestCase):
	def test_all_external_doctypes_link_source_site_to_configuration(self):
		external_doctypes = frappe.get_all(
			"DocType",
			filters=[
				["module", "=", "Emkan Insights"],
				["name", "like", "External %"],
				["istable", "=", 0],
				["name", "!=", "External Site Configuration"],
			],
			pluck="name",
		)

		self.assertTrue(external_doctypes)
		for doctype in external_doctypes:
			field = frappe.get_meta(doctype).get_field("source_site")
			self.assertIsNotNone(field, f"{doctype} has no source_site field")
			self.assertEqual(field.fieldtype, "Link", doctype)
			self.assertEqual(field.options, "External Site Configuration", doctype)

	@patch(
		"emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.frappe.log_error"
	)
	@patch(
		"emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.requests.Session"
	)
	def test_threaded_fetch_keeps_successes_and_aggregates_failures(
		self, session_class, log_error
	):
		class Response:
			def __init__(self, name):
				self.name = name

			def raise_for_status(self):
				return None

			def json(self):
				return {"data": {"name": self.name}}

		def get(url, **kwargs):
			if url.endswith("/missing"):
				raise ConnectionError("remote unavailable")
			return Response(url.rsplit("/", 1)[-1])

		session_class.return_value.get.side_effect = get

		docs, failures = _fetch_generic_docs_threaded(
			"https://remote.example",
			{"Authorization": "token key:secret"},
			"Item",
			["available", "missing"],
		)

		self.assertEqual(docs, [{"name": "available"}])
		self.assertEqual(len(failures), 1)
		self.assertEqual(failures[0]["remote_id"], "missing")
		log_error.assert_called_once()

	def test_fetch_only_doctypes_use_generic_fetcher(self):
		for remote_doctype, external_doctype in FETCH_ONLY_DOCTYPE_MAPPINGS.items():
			self.assertEqual(_GENERIC_DOCTYPE_MAP[remote_doctype], external_doctype)
			self.assertEqual(
				frappe.get_meta(external_doctype).get_field("json").fieldtype,
				"JSON",
			)

	def test_remote_payload_is_serialized_without_dropping_values(self):
		payload = {
			"name": "ACC-GL-0001",
			"posting_date": date(2026, 9, 7),
			"remarks": "دفعة اختبار",
			"dimensions": [{"cost_center": "Main"}],
		}

		stored_payload = json.loads(_serialize_remote_payload(payload))

		self.assertEqual(stored_payload["name"], payload["name"])
		self.assertEqual(stored_payload["posting_date"], "2026-09-07")
		self.assertEqual(stored_payload["remarks"], payload["remarks"])
		self.assertEqual(stored_payload["dimensions"], payload["dimensions"])

	def test_generic_fetch_saves_complete_payload_in_json_field(self):
		company = frappe.get_all("Company", pluck="name", limit=1)[0]
		remote_name = f"test-gl-entry-{frappe.generate_hash(length=8)}"
		payload = {
			"name": remote_name,
			"docstatus": 0,
			"modified": frappe.utils.now(),
			"account": "Remote Receivable",
			"debit": 125.75,
			"custom_dimensions": [{"dimension": "North"}],
		}
		local_fields = [
			field.fieldname for field in frappe.get_meta("External GL Entry").fields
		]
		errors = []

		saved = _process_item(
			"External GL Entry",
			payload,
			1,
			local_fields,
			company,
			None,
			errors,
			None,
		)

		self.assertTrue(saved, errors)
		name = frappe.db.get_value(
			"External GL Entry",
			{"remote_id": ["like", f"%-{remote_name}"]},
			"name",
		)
		stored_payload = json.loads(
			frappe.db.get_value("External GL Entry", name, "json")
		)
		self.assertEqual(stored_payload, payload)
