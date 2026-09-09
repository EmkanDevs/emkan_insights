# Copyright (c) 2026, Mukesh Variyani and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.emkan_insights.external_stock_entry_sync import (
	STOCK_ENTRY_PURPOSE_PRIORITY,
	_apply_item_link_mapping,
	_build_stock_timeline,
	_find_existing_stock_entry,
	_order_stock_entry_names,
	_process_stock_entry_chunk,
	_process_stock_timeline_batch,
	_resolve_stock_entry_reference,
	_sync_opening_stock_phase,
	_sync_pending_opening_stock_reconciliations,
)


class TestExternalStockEntry(FrappeTestCase):
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_find_existing_stock_entry",
		side_effect=[None, "CPC-EHC-MAT-STE-2025-00337"],
	)
	@patch(
		"frappe.get_all",
		return_value=["EHC-MAT-STE-2025-00337"],
	)
	def test_stock_entry_reference_resolves_source_prefixed_remote_id(
		self,
		get_all,
		find_existing,
	):
		result = _resolve_stock_entry_reference("MAT-STE-2025-00337", "CPC")

		self.assertEqual(result, "CPC-EHC-MAT-STE-2025-00337")
		get_all.assert_called_once()
		self.assertEqual(find_existing.call_count, 2)

	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_find_existing_stock_entry",
		return_value=None,
	)
	@patch(
		"frappe.get_all",
		return_value=["EHC-MAT-STE-2025-02244"],
	)
	def test_stock_entry_reference_keeps_external_prefix_before_target_exists(
		self,
		get_all,
		find_existing,
	):
		result = _resolve_stock_entry_reference("MAT-STE-2025-02244", "CPC")

		self.assertEqual(result, "CPC-EHC-MAT-STE-2025-02244")
		get_all.assert_called_once()
		self.assertEqual(find_existing.call_count, 2)

	@patch("frappe.db.commit")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_sync_pending_opening_stock_reconciliations",
		return_value=[{"status": "synced"}],
	)
	def test_completed_opening_stock_phase_is_committed(
		self,
		sync_opening_stock,
		commit,
	):
		result = _sync_opening_stock_phase(
			["STOCK-ENTRY-1"],
			"External Stock Entry",
		)

		self.assertEqual(result, [{"status": "synced"}])
		sync_opening_stock.assert_called_once_with(
			["STOCK-ENTRY-1"],
			"External Stock Entry",
		)
		commit.assert_called_once_with()

	@patch("frappe.db.commit")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_sync_pending_opening_stock_reconciliations",
		side_effect=frappe.ValidationError("opening stock failed"),
	)
	def test_failed_opening_stock_phase_is_not_committed(
		self,
		_sync_opening_stock,
		commit,
	):
		with self.assertRaises(frappe.ValidationError):
			_sync_opening_stock_phase(
				["STOCK-ENTRY-1"],
				"External Stock Entry",
			)

		commit.assert_not_called()

	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_get_materialized_opening_reconciliations",
		side_effect=[set(), {"OPENING-1", "OPENING-2"}],
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"sync_stock_reconciliation_docs"
	)
	@patch("frappe.get_meta")
	@patch("frappe.get_all")
	def test_pending_opening_stock_is_synced_before_stock_entries(
		self,
		get_all,
		get_meta,
		sync_reconciliations,
		_materialized,
	):
		get_meta.return_value.has_field.return_value = True
		get_all.side_effect = [
			[
				frappe._dict(
					company="Construction Pillars Company",
					source_site="CPC",
				)
			],
			[
				frappe._dict(
					name="OPENING-2",
					company="Construction Pillars Company",
					source_site="CPC",
					posting_date="2025-01-01",
					posting_time="09:00:00",
					modified="2026-09-01 10:00:00",
					last_synced_on=None,
				),
				frappe._dict(
					name="OPENING-1",
					company="Construction Pillars Company",
					source_site="CPC",
					posting_date="2025-01-01",
					posting_time="08:00:00",
					modified="2026-09-01 10:00:00",
					last_synced_on="2025-01-01 08:00:00",
				),
				frappe._dict(
					name="OTHER-SITE",
					company="Construction Pillars Company",
					source_site="OTHER",
					posting_date="2024-01-01",
					posting_time="00:00:00",
					modified="2026-09-01 10:00:00",
					last_synced_on=None,
				),
			],
		]

		_sync_pending_opening_stock_reconciliations(["STOCK-ENTRY-1"])

		sync_reconciliations.assert_called_once_with(
			source_doctype="External Stock Reconciliation",
			names=["OPENING-1", "OPENING-2"],
			sync_now=True,
		)

	def test_stock_entry_purpose_priority_matches_required_sequence(self):
		self.assertEqual(
			list(STOCK_ENTRY_PURPOSE_PRIORITY),
			[
				"Material Receipt",
				"Manufacture",
				"Material Transfer for Manufacture",
				"Material Transfer",
				"Repack",
				"Disassemble",
				"Material Consumption for Manufacture",
				"Send to Subcontractor",
				"Material Issue",
			],
		)

	@patch("frappe.get_all")
	def test_stock_entries_are_ordered_by_timestamp_then_purpose_for_ties(
		self, get_all
	):
		get_all.return_value = [
			frappe._dict(
				name="ISSUE-EARLY",
				posting_date="2024-11-03",
				posting_time="07:00:00",
				purpose="Material Issue",
			),
			frappe._dict(
				name="ISSUE-SAME-TIME",
				posting_date="2024-11-03",
				posting_time="08:00:00",
				purpose="Material Issue",
			),
			frappe._dict(
				name="RECEIPT-SAME-TIME",
				posting_date="2024-11-03",
				posting_time="08:00:00",
				purpose="Material Receipt",
			),
			frappe._dict(
				name="RECEIPT-LATER",
				posting_date="2024-11-04",
				posting_time="06:00:00",
				purpose="Material Receipt",
			),
		]

		ordered = _order_stock_entry_names(
			[
				"RECEIPT-LATER",
				"ISSUE-SAME-TIME",
				"RECEIPT-SAME-TIME",
				"ISSUE-EARLY",
			]
		)

		self.assertEqual(
			ordered,
			[
				"ISSUE-EARLY",
				"RECEIPT-SAME-TIME",
				"ISSUE-SAME-TIME",
				"RECEIPT-LATER",
			],
		)

	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_get_pending_regular_reconciliation_rows"
	)
	@patch("frappe.get_all")
	def test_stock_timeline_interleaves_reconciliations_chronologically(
		self,
		get_all,
		get_reconciliations,
	):
		get_all.return_value = [
			frappe._dict(
				name="ENTRY-1",
				posting_date="2025-01-04",
				posting_time="13:26:40",
				purpose="Material Transfer",
			)
		]
		get_reconciliations.return_value = [
			frappe._dict(
				name="RECON-BEFORE",
				posting_date="2025-01-02",
				posting_time="13:20:42",
			),
			frappe._dict(
				name="RECON-SAME-TIME",
				posting_date="2025-01-04",
				posting_time="13:26:40",
			),
			frappe._dict(
				name="RECON-AFTER",
				posting_date="2025-01-05",
				posting_time="08:00:00",
			),
		]

		timeline = _build_stock_timeline(
			["ENTRY-1"],
			"External Stock Entry",
		)

		self.assertEqual(
			[(event.kind, event.name) for event in timeline],
			[
				("reconciliation", "RECON-BEFORE"),
				("reconciliation", "RECON-SAME-TIME"),
				("stock_entry", "ENTRY-1"),
			],
		)

	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_process_stock_entry_chunk",
		return_value=[{"name": "ENTRY-1", "status": "synced"}],
	)
	@patch("frappe.db.commit")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"sync_stock_reconciliation_docs"
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_build_stock_timeline"
	)
	def test_stock_timeline_returns_only_stock_entry_results(
		self,
		build_timeline,
		sync_reconciliation,
		commit,
		process_stock_entry,
	):
		build_timeline.return_value = [
			frappe._dict(kind="reconciliation", name="RECON-1"),
			frappe._dict(kind="stock_entry", name="ENTRY-1"),
		]

		result = _process_stock_timeline_batch(
			["ENTRY-1"],
			"External Stock Entry",
		)

		self.assertEqual(result, [{"name": "ENTRY-1", "status": "synced"}])
		sync_reconciliation.assert_called_once_with(
			source_doctype="External Stock Reconciliation",
			names=["RECON-1"],
			sync_now=True,
		)
		process_stock_entry.assert_called_once_with(
			["ENTRY-1"],
			"External Stock Entry",
		)
		commit.assert_called_once_with()

	@patch("frappe.db.commit")
	@patch("frappe.db.set_value")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"sync_external_stock_entry",
		return_value={"name": "CPC-ENTRY-1", "status": "synced"},
	)
	@patch("frappe.get_doc")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"sync_record_guard"
	)
	def test_completed_stock_entry_and_source_marker_are_committed_together(
		self,
		record_guard,
		get_doc,
		sync_stock_entry,
		set_value,
		commit,
	):
		record_guard.return_value.__enter__.return_value = None
		get_doc.return_value = frappe._dict(
			company="Construction Pillars Company",
			modified="2026-09-07 13:19:53",
		)

		result = _process_stock_entry_chunk(
			["ENTRY-1"],
			"External Stock Entry",
		)

		self.assertEqual(result, [{"name": "CPC-ENTRY-1", "status": "synced"}])
		sync_stock_entry.assert_called_once_with(
			"ENTRY-1",
			"Construction Pillars Company",
		)
		set_value.assert_called_once_with(
			"External Stock Entry",
			"ENTRY-1",
			"last_synced_on",
			"2026-09-07 13:19:53",
			update_modified=False,
		)
		commit.assert_called_once_with()

	@patch("frappe.db.rollback")
	@patch("frappe.db.commit")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"log_record_failure"
	)
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"sync_external_stock_entry",
		side_effect=frappe.ValidationError("submit failed"),
	)
	@patch("frappe.get_doc")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"sync_record_guard"
	)
	def test_failed_stock_entry_is_rolled_back_without_commit(
		self,
		record_guard,
		get_doc,
		_sync_stock_entry,
		log_failure,
		commit,
		rollback,
	):
		record_guard.return_value.__enter__.return_value = None
		get_doc.return_value = frappe._dict(
			company="Construction Pillars Company",
			modified="2026-09-07 13:19:53",
		)

		with self.assertRaises(frappe.ValidationError):
			_process_stock_entry_chunk(
				["ENTRY-1"],
				"External Stock Entry",
			)

		commit.assert_not_called()
		rollback.assert_called_once_with()
		log_failure.assert_called_once_with(
			"External Stock Entry",
			"ENTRY-1",
			"submit failed",
		)

	@patch("frappe.log_error")
	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_resolve_material_request",
		return_value=None,
	)
	def test_unresolved_material_request_is_cleared(self, _resolve_mr, log_error):
		item_row = {
			"material_request": "MAT-MR-2024-00001",
			"material_request_item": "REMOTE-MR-ITEM-1",
		}

		_apply_item_link_mapping(item_row, "CPC", "EXT-STE-1")

		self.assertIsNone(item_row["material_request"])
		self.assertIsNone(item_row["material_request_item"])
		log_error.assert_called_once()

	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_get_stock_entry_remote_id_field",
		return_value=None,
	)
	@patch("frappe.db.exists", return_value=True)
	@patch("frappe.db.get_value")
	def test_existing_lookup_uses_name_when_remote_id_column_is_missing(
		self, get_value, _exists, _get_remote_id_field
	):
		result = _find_existing_stock_entry("REMOTE-SE-1", "COMP-REMOTE-SE-1")

		self.assertEqual(result, "COMP-REMOTE-SE-1")
		get_value.assert_not_called()

	@patch(
		"emkan_insights.emkan_insights.external_stock_entry_sync."
		"_get_stock_entry_remote_id_field",
		return_value="custom_remote_id",
	)
	@patch("frappe.db.exists")
	@patch("frappe.db.get_value", return_value="COMP-SE-1")
	def test_existing_lookup_uses_available_remote_id_column(
		self, get_value, exists, _get_remote_id_field
	):
		result = _find_existing_stock_entry("REMOTE-SE-1", "COMP-REMOTE-SE-1")

		self.assertEqual(result, "COMP-SE-1")
		get_value.assert_called_once_with(
			"Stock Entry", {"custom_remote_id": "REMOTE-SE-1"}, "name"
		)
		exists.assert_not_called()
