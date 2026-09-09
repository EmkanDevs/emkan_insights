import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_data_fetch import (
	FetchContext,
	_fetch_remote_names,
	_get_context,
	_validate_configuration,
)


MODULE = "emkan_insights.emkan_insights.external_data_fetch"


class TestExternalDataFetch(FrappeTestCase):
	@patch(f"{MODULE}._validate_staging_doctype")
	@patch(f"{MODULE}.frappe.utils.now_datetime", return_value="2026-09-09 12:00:00")
	@patch(f"{MODULE}.frappe.get_doc")
	@patch(f"{MODULE}.frappe.db.get_value")
	def test_context_uses_doctypes_from_configuration_row(
		self,
		get_value,
		get_doc,
		_now_datetime,
		validate_staging_doctype,
	):
		get_value.return_value = SimpleNamespace(
			name="row-1",
			parent="Remote Site",
			ref_doctype="Sales Invoice",
			exported_doctype="External Sales Invoice",
			last_sync="2026-09-08 12:00:00",
		)
		get_doc.return_value = frappe._dict(
			name="Remote Site",
			site_url="https://remote.example/",
			api_key="key",
			api_secret="secret",
		)

		context = _get_context("row-1")

		self.assertEqual(context.remote_doctype, "Sales Invoice")
		self.assertEqual(context.target_doctype, "External Sales Invoice")
		self.assertEqual(context.configuration, "Remote Site")
		self.assertEqual(context.site_url, "https://remote.example")
		validate_staging_doctype.assert_called_once_with("External Sales Invoice")

	def test_configuration_requires_remote_credentials(self):
		configuration = frappe._dict(
			name="Remote Site",
			site_url="https://remote.example",
			api_key="",
			api_secret="",
		)

		with self.assertRaises(frappe.ValidationError):
			_validate_configuration(configuration)

	@patch(f"{MODULE}.requests.get")
	def test_name_page_uses_fixed_incremental_window_and_cursor(self, get):
		response = Mock()
		response.json.return_value = {"data": [{"name": "SI-002"}]}
		get.return_value = response
		context = _fetch_context()

		names = _fetch_remote_names(context, "SI-001")

		self.assertEqual(names, ["SI-002"])
		params = get.call_args.kwargs["params"]
		self.assertEqual(
			json.loads(params["filters"]),
			[
				["modified", "<=", context.window_end],
				["modified", ">", context.window_start],
				["name", ">", "SI-001"],
			],
		)
		response.raise_for_status.assert_called_once_with()


def _fetch_context():
	return FetchContext(
		configuration="Remote Site",
		row="row-1",
		remote_doctype="Sales Invoice",
		target_doctype="External Sales Invoice",
		site_url="https://remote.example",
		api_key="key",
		api_secret="secret",
		window_start="2026-09-08 12:00:00",
		window_end="2026-09-09 12:00:00",
	)
