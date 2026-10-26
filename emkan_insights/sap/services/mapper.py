from __future__ import annotations

from math import isfinite
from typing import Any

import frappe

from emkan_insights.sap.services.transforms import (
	apply_transform,
	validate_sum_fields_config,
	validate_join_fields_config,
	validate_truncate_config,
)


class Mapper:
	def __init__(self, mapping_doc, context: dict[str, Any] | None = None):
		self.mapping_doc = mapping_doc
		self.field_mapping = parse_mapping(mapping_doc.field_mapping)
		self.context = context or {}

	def map(self, sap_record: dict[str, Any]) -> dict[str, Any]:
		if not isinstance(sap_record, dict):
			raise frappe.ValidationError("Each SAP record must be a JSON object.")

		result = {
			"doctype": self.mapping_doc.erp_doctype,
			**self._map_fields(sap_record, self.field_mapping),
		}
		self.fill_item_uoms(self.mapping_doc.erp_doctype, result)
		return result

	@staticmethod
	def fill_item_uoms(target_doctype: str, result: dict[str, Any]) -> None:
		parent_meta = frappe.get_meta(target_doctype)
		rows_by_item_code = {}
		for field in parent_meta.get_table_fields():
			rows = result.get(field.fieldname)
			if not rows:
				continue
			child_fields = {child_field.fieldname for child_field in frappe.get_meta(field.options).fields}
			uom_fields = child_fields.intersection({"stock_uom", "uom"})
			for row in rows:
				item_code = row.get("item_code")
				if item_code and any(not row.get(uom_field) for uom_field in uom_fields):
					rows_by_item_code.setdefault(item_code, []).append((row, uom_fields))

		if not rows_by_item_code:
			return

		items = frappe.get_all(
			"Item",
			filters={"item_code": ["in", list(rows_by_item_code)]},
			fields=["item_code", "stock_uom"],
			limit_page_length=0,
		)
		for item in items:
			if not item.stock_uom:
				continue
			for row, uom_fields in rows_by_item_code[item.item_code]:
				for uom_field in uom_fields:
					if not row.get(uom_field):
						row[uom_field] = item.stock_uom

	def _map_fields(
		self, source: dict[str, Any], mapping: dict[str, Any], parent_source: dict[str, Any] | None = None
	) -> dict[str, Any]:
		if not isinstance(source, dict):
			raise frappe.ValidationError("SAP parent and child records must be JSON objects.")

		result = {}
		for target_field, rule in mapping.items():
			if isinstance(rule, str):
				result[target_field] = source.get(rule)
				continue

			if "mapping" in rule or "rows" in rule:
				result[target_field] = self._map_child_rows(source, target_field, rule)
				continue

			value = source.get(rule.get("fieldname")) if rule.get("fieldname") else None
			mapped_value = apply_transform(
				value,
				rule,
				{**self.context, "source": source, "parent_source": parent_source, "target_field": target_field},
			)
			result[target_field] = rule.get("default") if mapped_value in (None, "") and "default" in rule else mapped_value
		return result

	def _map_child_rows(
		self, source: dict[str, Any], target_field: str, rule: dict[str, Any]
	) -> list[dict[str, Any]]:
		if "rows" in rule:
			mapped = [self._map_fields(source, normalize_mapping(row)) for row in rule["rows"]]
		else:
			rows = [source] if rule.get("source") == "parent" else source.get(rule["fieldname"]) or []
			if not isinstance(rows, list):
				raise frappe.ValidationError(f"SAP child field '{target_field}' must contain an array.")
			child_mapping = normalize_mapping(rule["mapping"])
			mapped = [self._map_fields(row, child_mapping, source) for row in rows]
		return self._filter_child_rows(mapped, rule)

	@staticmethod
	def _filter_child_rows(rows, rule):
		result, seen = [], {}
		for row in rows:
			if any(row.get(field) in (None, "") for field in rule.get("required_fields", [])):
				continue
			for field in rule.get("positive_fields", []):
				value = row.get(field)
				if (
					isinstance(value, bool)
					or not isinstance(value, (int, float))
					or not isfinite(value)
					or value <= 0
				):
					raise frappe.ValidationError(f"SAP child field '{field}' must be positive.")
			if unique_by := rule.get("unique_by"):
				key = tuple(row.get(field) for field in unique_by)
				if key in seen:
					if seen[key] != row:
						raise frappe.ValidationError("Conflicting SAP child rows share the configured unique key.")
					continue
				seen[key] = row
			result.append(row)
		return result


def parse_mapping(raw_mapping: Any) -> dict[str, Any]:
	try:
		mapping = frappe.parse_json(raw_mapping) if isinstance(raw_mapping, str) else raw_mapping
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"Field mapping is not valid JSON: {exc}") from exc

	return normalize_mapping(mapping)


def normalize_mapping(mapping: Any) -> dict[str, Any]:
	if isinstance(mapping, dict):
		result = mapping
	elif isinstance(mapping, list):
		result = _merge_mapping_rows(mapping)
	else:
		raise frappe.ValidationError("Field mapping must be a JSON object or an array of objects.")
	if not result:
		raise frappe.ValidationError("Field mapping cannot be empty.")

	for target_field, rule in result.items():
		_validate_rule(target_field, rule)
	return result


def _merge_mapping_rows(rows: list[Any]) -> dict[str, Any]:
	merged = {}
	for row in rows:
		if not isinstance(row, dict) or not row:
			raise frappe.ValidationError("Each mapping array entry must be a non-empty JSON object.")
		for target_field, rule in row.items():
			if target_field in merged:
				raise frappe.ValidationError(f"Duplicate ERPNext field in mapping: {target_field}")
			merged[target_field] = rule
	return merged


def _validate_rule(target_field: str, rule: Any) -> None:
	if not target_field:
		raise frappe.ValidationError("ERPNext fieldnames cannot be empty.")
	if isinstance(rule, str) and rule:
		return
	if not isinstance(rule, dict):
		raise frappe.ValidationError(f"Mapping for '{target_field}' must be a SAP fieldname or object.")
	transform = rule.get("transform") or "none"
	if transform not in {
		"none",
		"sap_date",
		"parent_date",
		"sap_bool",
		"sap_bool_not",
		"lookup",
		"payment_method_account",
		"local_payment_value",
		"item_conversion_factor",
		"sap_lookup",
		"sap_exists",
		"constant",
		"context",
		"value_map",
		"conditional_value",
		"first_value",
		"sum_fields",
		"join_fields",
		"truncate",
		"multiply",
		"sap_account_amount",
		"sap_account_currency",
	}:
		raise frappe.ValidationError(f"Unsupported SAP mapping transform: {transform}")
	if "mapping" in rule or "rows" in rule:
		if transform != "none":
			raise frappe.ValidationError(f"Child mapping '{target_field}' cannot define a transform.")
		if "default" in rule:
			raise frappe.ValidationError(f"Child mapping '{target_field}' cannot define a default.")
		_validate_child_rule(target_field, rule)
		return
	if "default" in rule and (rule["default"] is None or isinstance(rule["default"], (dict, list))):
		raise frappe.ValidationError(f"Default for '{target_field}' must be a scalar value.")
	if transform == "value_map":
		config = rule.get("transform_config") or {}
		if isinstance(config, str):
			try:
				config = frappe.parse_json(config)
			except (TypeError, ValueError) as exc:
				raise frappe.ValidationError(
					f"Value map for '{target_field}' requires valid JSON."
				) from exc
		if not isinstance(config, dict) or not isinstance(config.get("mapping"), dict):
			raise frappe.ValidationError(
				f"Value map for '{target_field}' requires a JSON mapping object."
			)
	if transform == "join_fields":
		validate_join_fields_config(rule)
	if transform == "sum_fields":
		validate_sum_fields_config(rule)
	if transform == "truncate":
		validate_truncate_config(rule)
	if transform == "multiply":
		factor = (rule.get("transform_config") or {}).get("factor")
		if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not isfinite(factor):
			raise frappe.ValidationError(f"Multiply for '{target_field}' requires a finite numeric factor.")
	if transform == "sap_account_amount" and not (rule.get("transform_config") or {}).get("base_field"):
		raise frappe.ValidationError(f"Account amount for '{target_field}' requires a base_field.")
	if transform in {"constant", "context", "first_value", "join_fields", "sum_fields", "payment_method_account", "local_payment_value", "item_conversion_factor"}:
		return
	if transform == "none" and "default" in rule and not rule.get("fieldname"):
		return
	if not rule.get("fieldname"):
		raise frappe.ValidationError(f"Mapping for '{target_field}' requires 'fieldname'.")


def _validate_child_rule(target_field, rule):
	for option in ("required_fields", "unique_by", "positive_fields"):
		if option in rule and (
			not isinstance(rule[option], list)
			or not all(isinstance(field, str) and field for field in rule[option])
		):
			raise frappe.ValidationError(f"{option} must be an array of fieldnames.")
	if "rows" in rule:
		if any(key in rule for key in ("mapping", "fieldname", "source")):
			raise frappe.ValidationError("Child rows cannot also define mapping, fieldname, or source.")
		if not isinstance(rule["rows"], list) or not rule["rows"]:
			raise frappe.ValidationError("Child rows must be a non-empty array of mappings.")
		for row in rule["rows"]:
			normalize_mapping(row)
	else:
		if rule.get("source") not in (None, "parent"):
			raise frappe.ValidationError("Child source must be parent when specified.")
		if rule.get("source") == "parent" and rule.get("fieldname"):
			raise frappe.ValidationError("A parent child mapping cannot also specify fieldname.")
		if rule.get("source") != "parent" and not rule.get("fieldname"):
			raise frappe.ValidationError(f"Child mapping '{target_field}' requires 'fieldname'.")
		normalize_mapping(rule["mapping"])
