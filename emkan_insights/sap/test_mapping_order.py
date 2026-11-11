from unittest import TestCase

from emkan_insights.sap.mapping_order import mapping_sort_key


class TestMappingOrder(TestCase):
	def test_document_references_follow_their_prerequisites(self):
		configured = (
			"SAP Delivery Note", "SAP Purchase Invoice", "SAP Purchase Order",
			"SAP Purchase Receipt", "SAP Sales Invoice", "SAP Sales Order",
			"SAP Supplier Quotation", "SAP Material Request", "SAP Quotation",
		)
		ordered = sorted(enumerate(configured), key=lambda pair: mapping_sort_key(pair[1], pair[0]))
		self.assertEqual([name for _, name in ordered], [
			"SAP Material Request", "SAP Supplier Quotation", "SAP Purchase Order",
			"SAP Purchase Receipt", "SAP Purchase Invoice", "SAP Quotation",
			"SAP Sales Order", "SAP Delivery Note", "SAP Sales Invoice",
		])
