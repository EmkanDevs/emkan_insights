"""SAP date mappings that need ERPNext-compatible operational dates."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.payment_dates import (
	INVOICE_MONTH_BASIS,
	get_due_date,
	get_due_date_from_template,
)
from emkan_insights.sap.services.mapper import Mapper


def catalog_mapping(name):
	path = Path(frappe.get_app_path("emkan_insights", "sap", "mapping_catalog.json"))
	return next(entry["field_mapping"] for entry in json.loads(path.read_text()) if entry["name"] == name)


class TestSAPDateMappings(FrappeTestCase):
	def test_request_keeps_sap_dates_when_required_date_precedes_transaction(self):
		mapping = catalog_mapping("SAP Material Request")
		mapper = Mapper(SimpleNamespace(erp_doctype="Material Request", field_mapping={
			"transaction_date": mapping["transaction_date"],
			"schedule_date": mapping["schedule_date"],
			"custom_sap_due_date": mapping["custom_sap_due_date"],
			"items": {"fieldname": "DocumentLines", "mapping": {
				"schedule_date": mapping["items"]["mapping"]["schedule_date"],
				"custom_sap_required_date": mapping["items"]["mapping"]["custom_sap_required_date"],
			}},
		}))
		result = mapper.map({
			"DocDate": "2024-03-06T00:00:00Z",
			"DocDueDate": "2024-02-28T00:00:00Z",
			"DocumentLines": [{"RequiredDate": "2024-02-28T00:00:00Z"}],
		})
		self.assertEqual(result["schedule_date"], "2024-03-06")
		self.assertEqual(result["items"][0]["schedule_date"], "2024-03-06")
		self.assertEqual(result["custom_sap_due_date"], "2024-02-28")
		self.assertEqual(result["items"][0]["custom_sap_required_date"], "2024-02-28")

	def test_payment_term_uses_calendar_months_after_invoice_date(self):
		mapping = catalog_mapping("SAP Payment Term")
		result = Mapper(SimpleNamespace(
			erp_doctype="Payment Term", field_mapping=mapping,
		)).map({
			"PaymentTermsGroupName": "ONE - INSTALMENT",
			"StartFrom": "pdt_None",
			"NumberOfAdditionalDays": 0,
			"NumberOfAdditionalMonths": 2,
		})
		self.assertEqual(result["due_date_based_on"], INVOICE_MONTH_BASIS)
		self.assertEqual(result["credit_months"], 2)
		self.assertEqual(
			get_due_date(frappe._dict(result), "2025-10-20"),
			"2025-12-20",
		)
		terms = SimpleNamespace(terms=[frappe._dict(result)])
		with patch("emkan_insights.sap.payment_dates.frappe.get_doc", return_value=terms):
			self.assertEqual(
				str(get_due_date_from_template("Example", "2025-10-20", None)),
				"2025-12-20",
			)
