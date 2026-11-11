from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.mapper import Mapper, normalize_mapping
from emkan_insights.sap.services.references import SAPReferenceResolver
from emkan_insights.sap.services.transforms import apply_transform
from emkan_insights.sap.services.transforms import journal_against, journal_cost_center, journal_party


class TestMappingReferences(FrappeTestCase):
	def test_journal_links_use_configured_party_and_cost_center_prefixes(self):
		context = {
			"company": "Petroleum Vision Contracting Company", "company_abbr": "PV",
			"prefixed_doctypes": frozenset({"Supplier", "Cost Center"}),
		}
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=None), patch(
			"emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True,
		):
			self.assertEqual(journal_against("V00274", context), "PV-V00274")
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=SimpleNamespace(
			company="Petroleum Vision Contracting Company", is_group=0,
		)) as get_value:
			self.assertEqual(journal_cost_center("01", context), "PV-01")
		get_value.assert_called_once_with("Cost Center", "PV-01", ["company", "is_group"], as_dict=True)

	def test_journal_party_uses_scoped_supplier_and_skips_non_party_account(self):
		context = {
			"source": {"AccountCode": "2131102"},
			"company": "Petroleum Vision Contracting Company",
			"company_abbr": "PV", "prefixed_doctypes": frozenset({"Supplier"}),
			"target_field": "party",
		}
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=SimpleNamespace(
			name="Payables", account_type="Payable",
		)), patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True) as exists:
			self.assertEqual(journal_party("V00118", context), "PV-V00118")
		exists.assert_called_once_with("Supplier", "PV-V00118")
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=SimpleNamespace(
			name="Supplier Advances", account_type=None,
		)):
			self.assertIsNone(journal_party("V00118", context))

	def test_scoped_item_lookup_uses_prefixed_unique_code(self):
		context = {"company_abbr": "PV", "prefixed_doctypes": frozenset({"Item"})}
		rule = {"transform": "lookup", "transform_config": {
			"doctype": "Item", "field": "item_code", "required": True,
		}}
		with patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value="PV-SR000001") as get_value:
			self.assertEqual(apply_transform("SR000001", rule, context), "PV-SR000001")
		get_value.assert_called_once_with("Item", {"item_code": "PV-SR000001"}, "name")

	def test_account_parent_follows_sap_father_account(self):
		rule = {"transform": "sap_account_parent"}
		resolver = Mock()
		resolver.resolve.return_value = "Non-Operating Income and Expenditure"
		with patch("emkan_insights.sap.services.transforms.lookup", return_value=None):
			for code in ("7200000", "7800000"):
				context = {"company": "Samif", "source": {"Code": code}, "sap_references": resolver}
				self.assertEqual(
					apply_transform("700000000000000", rule, context),
					"Non-Operating Income and Expenditure - 700000000000000",
				)
		resolver.resolve.assert_called()

	def test_purchase_invoice_lines_resolve_receipt_and_receipt_item(self):
		client = Mock()
		client.get.return_value = {"value": [{"DocumentLines": [
			{"LineNum": 0, "ItemCode": "ITEM-1"},
			{"LineNum": 1, "ItemCode": "ITEM-2"},
		]}]}
		resolver = SAPReferenceResolver(client)
		source = {"BaseType": 20, "BaseEntry": 45, "BaseLine": 1, "ItemCode": "ITEM-2"}
		context = {
			"source": source, "sap_references": resolver, "company": "Company A",
			"configuration_name": "Emkan", "company_abbr": "A",
			"prefixed_doctypes": frozenset({"Purchase Receipt", "Item"}),
		}
		def get_value(doctype, filters, fieldname, **kwargs):
			if doctype == "Purchase Receipt":
				self.assertEqual(filters, {"name": "A-45", "company": "Company A"})
				return "A-45"
			self.assertEqual(filters, {"parent": "A-45", "idx": 2})
			return SimpleNamespace(name="receipt-item-2", item_code="A-ITEM-2")

		with patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True), patch(
			"emkan_insights.sap.services.transforms.frappe.db.get_value", side_effect=get_value
		):
			self.assertEqual(apply_transform(None, {"transform": "purchase_invoice_receipt_link"}, {
				**context, "target_field": "purchase_receipt",
			}), "A-45")
			self.assertEqual(apply_transform(None, {"transform": "purchase_invoice_receipt_link"}, {
				**context, "target_field": "pr_detail",
			}), "receipt-item-2")
		client.get.assert_called_once()

	def test_purchase_order_request_line_compares_scoped_item_code(self):
		resolver = Mock()
		resolver.purchase_request_line.return_value = ("57", 1)
		context = {
			"source": {"ItemCode": "MA001941"},
			"sap_references": resolver,
			"configuration_name": "Petroleum Vision Contracting Company",
			"company": "Petroleum Vision Contracting Company", "company_abbr": "PV",
			"prefixed_doctypes": frozenset({"Item"}),
			"target_field": "material_request_item",
		}
		def get_value(doctype, filters, fields, **kwargs):
			if doctype == "Company":
				return "PV"
			if doctype == "Material Request":
				return "PV-57"
			return SimpleNamespace(name="request-line", item_code="PV-MA001941")

		with patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True), patch(
			"emkan_insights.sap.services.transforms.frappe.db.get_value", side_effect=get_value,
		):
			self.assertEqual(apply_transform(None, {"transform": "purchase_order_request_link"}, context), "request-line")

	def test_purchase_invoice_receipt_link_rejects_missing_target_and_wrong_item(self):
		client = Mock()
		client.get.return_value = {"value": [{"DocumentLines": [
			{"LineNum": 0, "ItemCode": "ITEM-1"},
		]}]}
		context = {
			"source": {"BaseType": 20, "BaseEntry": 45, "BaseLine": 0, "ItemCode": "ITEM-1"},
			"sap_references": SAPReferenceResolver(client), "company": "Company A",
			"configuration_name": "Emkan", "company_abbr": "A",
			"prefixed_doctypes": frozenset({"Purchase Receipt"}),
			"target_field": "purchase_receipt",
		}
		rule = {"transform": "purchase_invoice_receipt_link"}
		with patch("emkan_insights.sap.services.transforms.frappe.db.exists", return_value=True), patch(
			"emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=None
		):
			with self.assertRaisesRegex(frappe.ValidationError, "ERPNext Purchase Receipt"):
				apply_transform(None, rule, context)
		context["source"] = {**context["source"], "ItemCode": "OTHER"}
		with self.assertRaisesRegex(frappe.ValidationError, "differs from receipt"):
			apply_transform(None, rule, context)
		context["source"]["BaseType"] = -1
		self.assertIsNone(apply_transform(None, rule, context))

	def test_purchase_order_lines_follow_direct_and_quotation_request_references(self):
		def sap_get(entity, params):
			if entity == "PurchaseQuotations":
				return {"value": [{"DocumentLines": [
					{"LineNum": 3, "ItemCode": "ITEM-1", "BaseType": 1470000113, "BaseEntry": 405, "BaseLine": 1},
				]}]}
			return {"value": [{"DocumentLines": [
				{"LineNum": 1, "ItemCode": "ITEM-1"},
				]}]}

		client = Mock()
		client.get.side_effect = sap_get
		resolver = SAPReferenceResolver(client)
		direct = {"BaseType": 1470000113, "BaseEntry": 405, "BaseLine": 1, "ItemCode": "ITEM-1"}
		quotation = {"BaseType": 540000006, "BaseEntry": 50, "BaseLine": 3, "ItemCode": "ITEM-1"}
		self.assertEqual(resolver.purchase_request_line(direct), ("405", 1))
		self.assertEqual(resolver.purchase_request_line(quotation), ("405", 1))
		self.assertEqual(client.get.call_count, 2)

	def test_caches_lookups_only_for_this_connection(self):
		config = {"entity": "Groups", "key": "Code", "value_field": "Name", "key_type": "integer"}
		first = Mock()
		first.get.return_value = {"value": [{"Name": "First company group"}]}
		second = Mock()
		second.get.return_value = {"value": [{"Name": "Second company group"}]}
		resolver = SAPReferenceResolver(first)
		self.assertEqual(resolver.resolve(1, config), "First company group")
		self.assertEqual(resolver.resolve(1, config), "First company group")
		self.assertEqual(SAPReferenceResolver(second).resolve(1, config), "Second company group")
		first.get.assert_called_once_with(
			"Groups", params={"$filter": "Code eq 1", "$select": "Name", "$top": 2}
		)

	def test_escapes_string_keys_and_skips_sentinels(self):
		client = Mock()
		client.get.return_value = {"value": [{"Name": "Example"}]}
		resolver = SAPReferenceResolver(client)
		config = {"entity": "Groups", "key": "Code", "value_field": "Name", "empty_values": [-1]}
		self.assertIsNone(resolver.resolve(-1, config))
		self.assertIsNone(resolver.resolve(None, config))
		client.get.assert_not_called()
		resolver.resolve("O'Neil", config)
		self.assertEqual(client.get.call_args.kwargs["params"]["$filter"], "Code eq 'O''Neil'")

	def test_checks_sap_reference_existence_with_an_extra_filter(self):
		client = Mock()
		client.get.return_value = {"value": [{"PriceListNum": 1}]}
		resolver = SAPReferenceResolver(client)
		config = {
			"entity": "BusinessPartners",
			"key": "PriceListNum",
			"key_type": "integer",
			"filter": "CardType eq 'cCustomer'",
		}

		self.assertEqual(resolver.exists(1, config), 1)
		self.assertEqual(resolver.exists(1, config), 1)
		client.get.assert_called_once_with(
			"BusinessPartners",
			params={
				"$filter": "(PriceListNum eq 1) and (CardType eq 'cCustomer')",
				"$select": "PriceListNum",
				"$top": 1,
			},
		)

	def test_rejects_missing_ambiguous_or_malformed_references(self):
		config = {"entity": "Groups", "key": "Code", "value_field": "Name"}
		for rows in ([], [{"Name": "A"}, {"Name": "B"}], [{}]):
			client = Mock()
			client.get.return_value = {"value": rows}
			with self.assertRaises(frappe.ValidationError):
				SAPReferenceResolver(client).resolve("1", config)
		for invalid in ({"entity": "https://other.example"}, {"key_type": "integer"}):
			with self.assertRaises(frappe.ValidationError):
				SAPReferenceResolver(Mock()).resolve("not-an-integer", {**config, **invalid})

	def test_maps_parent_into_company_defaults(self):
		client = Mock()
		client.get.return_value = {"value": [{"Title": "Main Store"}]}
		mapping = {
			"item_defaults": {
				"source": "parent",
				"mapping": {
					"company": {"transform": "context", "transform_config": {"key": "company"}},
					"default_warehouse": {
						"fieldname": "Warehouse",
						"transform": "sap_lookup",
						"transform_config": {"entity": "Stores", "key": "Code", "value_field": "Title"},
					},
				},
			}
		}
		mapper = Mapper(
			SimpleNamespace(erp_doctype="Item", field_mapping=mapping),
			context={"company": "Company A", "sap_references": SAPReferenceResolver(client)},
		)
		self.assertEqual(
			mapper.map({"Warehouse": "01"})["item_defaults"],
			[{"company": "Company A", "default_warehouse": "Main Store"}],
		)

	def test_deduplicates_generated_rows_and_skips_blank_units(self):
		mapping = {
			"uoms": {
				"rows": [
					{"uom": "Stock", "conversion_factor": {"transform": "constant", "value": 1}},
					{"uom": "Purchase", "conversion_factor": "Factor"},
				],
				"required_fields": ["uom"],
				"unique_by": ["uom"],
			}
		}
		mapper = Mapper(SimpleNamespace(erp_doctype="Item", field_mapping=mapping))
		for purchase in (None, "Unit"):
			self.assertEqual(
				mapper.map({"Stock": "Unit", "Purchase": purchase, "Factor": 1})["uoms"],
				[{"uom": "Unit", "conversion_factor": 1}],
			)
		mapped = mapper.map({"Stock": "Unit", "Purchase": "Unit", "Factor": 12})
		self.assertEqual(mapped["uoms"], [])
		self.assertIn("Conflicting SAP child rows", mapped["_sap_mapping_errors"][0]["message"])

	def test_rejects_malformed_child_rules(self):
		for rule in (
			{"rows": []},
			{"rows": [{"uom": "Unit"}], "mapping": {"uom": "Unit"}},
			{"source": "unknown", "mapping": {"uom": "Unit"}},
			{"source": "parent", "mapping": {"uom": "Unit"}, "unique_by": "uom"},
		):
			with self.assertRaises(frappe.ValidationError):
				normalize_mapping({"uoms": rule})

	def test_rejects_nonpositive_conversion_factors(self):
		mapper = Mapper(SimpleNamespace(
			erp_doctype="Item",
			field_mapping={
				"uoms": {
					"source": "parent",
					"mapping": {"conversion_factor": "Factor"},
					"positive_fields": ["conversion_factor"],
				}
			},
		))
		for value in (0, -1, None, "12", True, float("nan"), float("inf")):
			mapped = mapper.map({"Factor": value})
			self.assertEqual(mapped["uoms"], [])
			self.assertIn("must be positive", mapped["_sap_mapping_errors"][0]["message"])

	def test_sap_lookup_requires_a_connection(self):
		with self.assertRaises(frappe.ValidationError):
			apply_transform(1, {"transform": "sap_lookup"})

	@patch("emkan_insights.sap.services.transforms.frappe.db.get_value", return_value=None)
	def test_required_local_reference_fails_if_missing(self, _get_value):
		with self.assertRaises(frappe.ValidationError):
			apply_transform("XX", {
				"transform": "lookup",
				"transform_config": {"doctype": "Country", "field": "code", "required": True},
			})

	def test_strict_value_map_rejects_unknown_units(self):
		rule = {"transform": "value_map", "transform_config": {
			"mapping": {"1": "Unit"}, "strict": True, "default": None,
		}}
		self.assertEqual(apply_transform(1, rule), "Unit")
		self.assertIsNone(apply_transform(None, rule))
		with self.assertRaises(frappe.ValidationError):
			apply_transform(2, rule)

	def test_validates_generated_child_fields_against_target_schema(self):
		from emkan_insights.sap.doctype.sap_doctype_mapping.sap_doctype_mapping import SAPDoctypeMapping

		doc = SAPDoctypeMapping({"doctype": "SAP Doctype Mapping"})
		doc._validate_target_fields(frappe.get_meta("Item"), {
			"uoms": {"rows": [{"uom": "Unit", "conversion_factor": "Factor"}]}
		})
		with self.assertRaises(frappe.ValidationError):
			doc._validate_target_fields(frappe.get_meta("Item"), {
				"uoms": {"rows": [{"not_a_real_field": "Unit"}]}
			})
