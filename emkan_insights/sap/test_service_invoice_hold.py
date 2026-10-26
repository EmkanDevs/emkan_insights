from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services import _invoice_sync_hold_reason, _process_record, _sync_hold_reason
from emkan_insights.sap.sync import _prepare_source_row


class TestServiceInvoiceHold(FrappeTestCase):
	def test_service_invoice_is_held_with_original_sap_record(self):
		mapping = SimpleNamespace(
			name="SAP Sales Invoice", sap_doctype="Invoices", sap_id_field="DocEntry",
			sap_id_prefix="", raw_payload=False,
		)
		record = {
			"DocEntry": 42,
			"DocType": "dDocument_Service",
			"DocumentLines": [{"AccountCode": "4000", "ItemCode": None}],
		}
		writer = Mock()
		writer.upsert.return_value = ("created", "STAGE-1")
		mapper = Mock()
		result = {"success": [], "created": [], "updated": [], "unchanged": [], "failed": []}

		_process_record(mapping, mapper, writer, record, 0, result)

		mapper.map.assert_not_called()
		payload = writer.upsert.call_args.args[1]
		self.assertEqual(payload["record"], record)
		self.assertEqual(payload["source_endpoint"], "Invoices")
		self.assertEqual(writer.upsert.call_args.kwargs["sync_hold_reason"],
			"Service invoice needs an ERP item mapping")
		self.assertEqual(result["created"], ["42"])

	def test_item_invoice_is_ready_for_sync(self):
		mapping = SimpleNamespace(name="SAP Sales Invoice")
		with patch("frappe.db.get_value", return_value="Receivable"):
			self.assertIsNone(_invoice_sync_hold_reason(mapping, {
				"DocType": "dDocument_Items", "ControlAccount": "1131101",
				"DocumentLines": [{"ItemCode": "ITEM-1"}],
			}))

	def test_item_purchase_invoice_needs_payable_account_type(self):
		mapping = SimpleNamespace(name="SAP Purchase Invoice")
		def account_lookup(doctype, *args, **kwargs):
			if doctype == "Item":
				return SimpleNamespace(is_purchase_item=1, stock_uom="EACH")
			return None
		with patch("frappe.db.get_value", side_effect=account_lookup):
			self.assertEqual(_invoice_sync_hold_reason(mapping, {
				"DocType": "dDocument_Items", "ControlAccount": "2131102",
				"DocumentLines": [{"ItemCode": "ITEM-1", "UoMCode": "EACH"}],
			}), "Invoice control account needs ERP Payable account type")

	def test_invoice_with_missing_item_code_is_held(self):
		mapping = SimpleNamespace(name="SAP Sales Invoice")
		self.assertEqual(_invoice_sync_hold_reason(mapping, {
			"DocType": "dDocument_Items",
			"DocumentLines": [{"ItemCode": "ITEM-1"}, {"ItemCode": None}],
		}), "Invoice line has no SAP item code")

	def test_purchase_service_invoice_is_held(self):
		mapping = SimpleNamespace(name="SAP Purchase Invoice")
		self.assertEqual(_invoice_sync_hold_reason(mapping, {
			"DocType": "dDocument_Service", "DocumentLines": [{"ItemCode": None}],
		}), "Service invoice needs an ERP item mapping")

	def test_other_mappings_are_not_held(self):
		mapping = SimpleNamespace(name="SAP Purchase Order")
		self.assertIsNone(_invoice_sync_hold_reason(mapping, {
			"DocType": "dDocument_Service", "DocumentLines": [{"ItemCode": None}],
		}))

	def test_payment_is_held_until_account_mapping_exists(self):
		for name in ("SAP Payment Entry Incoming", "SAP Payment Entry Outgoing"):
			with self.subTest(name=name):
				self.assertEqual(
					_sync_hold_reason(SimpleNamespace(name=name), {"DocEntry": 42, "DocType": "rSupplier"}),
					"Payment account types, exchange rates, and allocations need verification",
				)

	def test_account_payment_has_separate_hold_reason(self):
		self.assertEqual(
			_sync_hold_reason(SimpleNamespace(name="SAP Payment Entry Outgoing"), {"DocType": "rAccount"}),
			"SAP account payment needs an ERP general ledger mapping",
		)

	def test_raw_held_invoice_cannot_be_imported_if_hold_field_is_cleared(self):
		context = SimpleNamespace(mapping_name="SAP Sales Invoice")
		source = SimpleNamespace(
			name="STAGE-1",
			json=frappe.as_json({
				"sap_mapping": "SAP Sales Invoice",
				"source_endpoint": "Invoices",
				"record": {"DocType": "dDocument_Service"},
			}),
		)
		with self.assertRaisesRegex(frappe.ValidationError, "needs a verified ERP mapping"):
			_prepare_source_row(context, source)
