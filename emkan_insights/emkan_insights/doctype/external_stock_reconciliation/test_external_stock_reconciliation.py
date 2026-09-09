# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import frappe
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	EmptyStockReconciliationItemsError,
)
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_stock_reconciliation_sync import (
	_ensure_cancelled_amendment_parent,
	_insert_and_sync_reconciliation,
	_safe_cancel_doc,
	_safe_submit_doc,
	_sync_stock_reconciliation_docs_job,
)


class TestExternalStockReconciliation(FrappeTestCase):
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync._log"
	)
	def test_reconciliation_with_no_changes_is_skipped(self, log):
		doc = MagicMock()
		doc.insert.side_effect = EmptyStockReconciliationItemsError(
			"None of the items have any change in quantity or value."
		)

		result = _insert_and_sync_reconciliation(
			doc,
			"TARGET-RECO-1",
			"SOURCE-RECO-1",
			2,
		)

		self.assertEqual(
			result,
			{
				"name": "TARGET-RECO-1",
				"status": "skipped",
				"reason": "No change in quantity or value",
			},
		)
		doc.insert.assert_called_once_with(ignore_permissions=True)
		log.assert_called_once()

	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"temporarily_enable_transaction_items",
		side_effect=lambda *_args: nullcontext(),
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"active_item_during_opening_stock",
		side_effect=lambda *_args: nullcontext(),
	)
	def test_safe_submit_uses_synchronous_action(self, _active_item, _enable_items):
		doc = MagicMock()

		_safe_submit_doc(doc)

		doc._submit.assert_called_once_with()
		doc.submit.assert_not_called()

	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"temporarily_enable_transaction_items",
		side_effect=lambda *_args: nullcontext(),
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"active_item_during_opening_stock",
		side_effect=lambda *_args: nullcontext(),
	)
	def test_safe_cancel_uses_synchronous_action(self, _active_item, _enable_items):
		doc = MagicMock()

		_safe_cancel_doc(doc)

		doc._cancel.assert_called_once_with()
		doc.cancel.assert_not_called()

	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync.sleep"
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"temporarily_enable_transaction_items",
		side_effect=lambda *_args: nullcontext(),
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"sync_record_guard",
		side_effect=lambda *_args: nullcontext(),
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"sync_external_stock_reconciliation",
		side_effect=[frappe.QueryDeadlockError("deadlock"), {"status": "synced"}],
	)
	@patch("frappe.db.set_value")
	@patch("frappe.db.rollback")
	@patch("frappe.get_doc")
	def test_deadlock_rolls_back_and_retries_complete_batch(
		self,
		get_doc,
		rollback,
		_set_value,
		sync_reconciliation,
		_sync_guard,
		_enable_items,
		sleep,
	):
		get_doc.return_value = frappe._dict(
			company="Test Company",
			modified="2026-09-05 10:00:00",
			items=[],
		)

		result = _sync_stock_reconciliation_docs_job(
			source_doctype="External Stock Reconciliation",
			names=["OPENING-1"],
		)

		self.assertEqual(result, [{"status": "synced"}])
		rollback.assert_called_once_with()
		sleep.assert_called_once_with(2)
		self.assertEqual(get_doc.call_count, 2)
		self.assertEqual(sync_reconciliation.call_count, 2)

	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"_sync_external_stock_reconciliation"
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"sync_record_guard",
		side_effect=lambda *_args, **_kwargs: nullcontext(),
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"_resolve_external_stock_reconciliation",
		return_value="PARENT-EXT",
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"_find_existing_stock_reconciliation",
		side_effect=["PARENT-LOCAL", "PARENT-LOCAL"],
	)
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value", side_effect=[1, 2])
	def test_amendment_parent_is_synced_and_cancelled_first(
		self,
		get_value,
		get_doc,
		_find_existing,
		_resolve_external,
		_sync_guard,
		sync_parent,
	):
		external = frappe._dict(
			name="CHILD-EXT",
			amended_from="PARENT-EXT",
		)
		get_doc.return_value = frappe._dict(company="Test Company")

		result = _ensure_cancelled_amendment_parent(
			external,
			"TC",
			("CHILD-EXT",),
		)

		self.assertEqual(result, "PARENT-LOCAL")
		sync_parent.assert_called_once_with(
			"PARENT-EXT",
			"Test Company",
			("CHILD-EXT",),
		)
		self.assertEqual(get_value.call_count, 2)

	@patch(
		"emkan_insights.emkan_insights.external_stock_reconciliation_sync."
		"_find_existing_stock_reconciliation",
		return_value="PARENT-LOCAL",
	)
	@patch("frappe.db.get_value", return_value=2)
	def test_cancelled_amendment_parent_is_reused(self, _get_value, _find_existing):
		external = frappe._dict(
			name="CHILD-EXT",
			amended_from="PARENT-EXT",
		)

		result = _ensure_cancelled_amendment_parent(
			external,
			"TC",
			("CHILD-EXT",),
		)

		self.assertEqual(result, "PARENT-LOCAL")
