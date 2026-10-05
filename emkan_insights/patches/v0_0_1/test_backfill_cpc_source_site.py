import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.patches.v0_0_1.backfill_cpc_source_site import (
	_cpc_source_site_condition,
)


class TestBackfillCpcSourceSite(FrappeTestCase):
	def test_multi_source_site_requires_cpc_attribution(self):
		table = frappe.qb.DocType("External Purchase Order")
		condition = _cpc_source_site_condition(
			table,
			{"source_site", "site_url"},
			"https://cpc.example.com/",
			False,
		)

		sql = frappe.qb.from_(table).select(table.name).where(condition).get_sql()
		self.assertIn("`source_site` IS NULL", sql)
		self.assertIn("`site_url` IN ('https://cpc.example.com','https://cpc.example.com/')", sql)

	def test_single_source_site_accepts_blank_source_site(self):
		table = frappe.qb.DocType("External Purchase Order")
		condition = _cpc_source_site_condition(
			table,
			{"source_site"},
			"",
			True,
		)

		sql = frappe.qb.from_(table).select(table.name).where(condition).get_sql()
		self.assertIn("`source_site` IS NULL", sql)

	def test_multi_source_site_without_cpc_url_is_not_repaired(self):
		table = frappe.qb.DocType("External Purchase Order")

		condition = _cpc_source_site_condition(table, {"source_site"}, "", False)

		self.assertIsNone(condition)
