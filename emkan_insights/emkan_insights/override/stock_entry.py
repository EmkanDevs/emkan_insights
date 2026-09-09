import frappe
from frappe import _
from frappe.utils import flt

from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry as ERPNextStockEntry


class StockEntry(ERPNextStockEntry):
	def update_transferred_qty(self):
		if self.purpose != "Material Transfer" or not self.outgoing_stock_entry:
			return

		stock_entries = {}
		stock_entries_child_list = []
		for row in self.items:
			if not (row.against_stock_entry and row.ste_detail):
				continue

			stock_entries_child_list.append(row.ste_detail)
			transferred_qty = frappe.get_all(
				"Stock Entry Detail",
				fields=["sum(transfer_qty) as qty"],
				filters={
					"against_stock_entry": row.against_stock_entry,
					"ste_detail": row.ste_detail,
					"docstatus": 1,
				},
			)

			if row.docstatus == 1:
				requested_qty = frappe.get_value(
					"Stock Entry Detail",
					row.ste_detail,
					"transfer_qty",
				)
				# Remote child-row names may not exist locally. In that case,
				# use this transfer row's numeric quantity as the safe limit.
				transfer_qty = flt(
					requested_qty if requested_qty is not None else row.transfer_qty
				)
				if transferred_qty and flt(transferred_qty[0].qty) > transfer_qty:
					frappe.throw(
						_(
							"Row {0}: Transferred quantity cannot be greater than the requested quantity."
						).format(row.idx)
					)

			stock_entries[(row.against_stock_entry, row.ste_detail)] = (
				flt(transferred_qty[0].qty) if transferred_qty else 0.0
			)

		if not stock_entries:
			return

		conditions = ""
		for reference, transferred_qty in stock_entries.items():
			conditions += """ WHEN (parent = {} and name = {}) THEN {}
				""".format(
				frappe.db.escape(reference[0]),
				frappe.db.escape(reference[1]),
				transferred_qty,
			)

		if stock_entries_child_list:
			frappe.db.sql(
				""" UPDATE `tabStock Entry Detail`
				SET
					transferred_qty = CASE {conditions} END
				WHERE
					name in ({ste_details}) """.format(
					conditions=conditions,
					ste_details=",".join(["%s"] * len(stock_entries_child_list)),
				),
				tuple(stock_entries_child_list),
			)

		self._update_percent_field_in_targets(
			{
				"source_dt": "Stock Entry Detail",
				"target_field": "transferred_qty",
				"target_ref_field": "qty",
				"target_dt": "Stock Entry Detail",
				"join_field": "ste_detail",
				"target_parent_dt": "Stock Entry",
				"target_parent_field": "per_transferred",
				"source_field": "qty",
				"percent_join_field": "against_stock_entry",
			},
			update_modified=True,
		)
