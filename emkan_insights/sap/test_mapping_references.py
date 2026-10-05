from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from emkan_insights.sap.services.mapper import Mapper, normalize_mapping
from emkan_insights.sap.services.references import SAPReferenceResolver
from emkan_insights.sap.services.transforms import apply_transform


class TestMappingReferences(FrappeTestCase):
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
		with self.assertRaises(frappe.ValidationError):
			mapper.map({"Stock": "Unit", "Purchase": "Unit", "Factor": 12})

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
			with self.assertRaises(frappe.ValidationError):
				mapper.map({"Factor": value})

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
