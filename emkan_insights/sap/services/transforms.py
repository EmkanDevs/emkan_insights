from __future__ import annotations

from typing import Any

import frappe


def apply_transform(value: Any, rule: dict[str, Any], context: dict[str, Any] | None = None) -> Any:
	transform = rule.get("transform") or "none"

	if transform == "none":
		return value
	if transform == "sap_date":
		return sap_date(value)
	if transform == "sap_bool":
		return sap_bool(value)
	if transform == "sap_bool_not":
		return int(not sap_bool(value))
	if transform == "lookup":
		return lookup(value, rule)
	if transform == "sap_lookup":
		resolver = (context or {}).get("sap_references")
		if resolver is None:
			raise frappe.ValidationError("SAP lookup requires a source connection.")
		return resolver.resolve(value, _transform_config(rule))
	if transform == "sap_exists":
		resolver = (context or {}).get("sap_references")
		if resolver is None:
			raise frappe.ValidationError("SAP exists requires a source connection.")
		return resolver.exists(value, _transform_config(rule))
	if transform == "constant":
		return constant(rule)
	if transform == "context":
		return context_value(rule, context or {})
	if transform == "value_map":
		return value_map(value, rule)
	if transform == "conditional_value":
		return conditional_value(rule, context or {})
	if transform == "first_value":
		return first_value(rule, context or {})

	raise frappe.ValidationError(f"Unsupported SAP mapping transform: {transform}")


def sap_date(value: Any) -> str | None:
	if not value:
		return None
	return str(value)[:10]


def sap_bool(value: Any) -> int:
	if isinstance(value, bool):
		return int(value)
	return int(str(value or "").strip().lower() in {"tyes", "yes", "y", "true", "1"})


def lookup(value: Any, rule: dict[str, Any]) -> str | None:
	if value in (None, ""):
		return None

	config = _transform_config(rule)
	doctype = config.get("doctype")
	fieldname = config.get("field")
	if not doctype or not fieldname:
		raise frappe.ValidationError("The lookup transform requires 'doctype' and 'field'.")

	name = frappe.db.get_value(doctype, {fieldname: value}, "name")
	if name is None and config.get("required"):
		raise frappe.ValidationError(f"ERPNext reference {doctype}.{fieldname} was not found.")
	return name


def constant(rule: dict[str, Any]) -> Any:
	return _transform_config(rule).get("value")


def context_value(rule: dict[str, Any], context: dict[str, Any]) -> Any:
	key = _transform_config(rule).get("key")
	if not key:
		raise frappe.ValidationError("The context transform requires a 'key'.")
	return context.get(key)


def value_map(value: Any, rule: dict[str, Any]) -> Any:
	config = _transform_config(rule)
	mapping = config.get("mapping") or {}
	if not isinstance(mapping, dict):
		raise frappe.ValidationError("The value_map transform requires a 'mapping' object.")
	if config.get("strict") and value not in (None, "") and str(value) not in mapping:
		raise frappe.ValidationError("The SAP value has no configured ERPNext equivalent.")
	return mapping.get(str(value), config.get("default", value))


def conditional_value(rule: dict[str, Any], context: dict[str, Any]) -> Any:
	config = _transform_config(rule)
	conditions = config.get("conditions")
	source = context.get("source")
	if not isinstance(conditions, list) or not conditions:
		raise frappe.ValidationError(
			"The conditional_value transform requires a non-empty 'conditions' array."
		)
	if not isinstance(source, dict):
		raise frappe.ValidationError("The conditional_value transform requires the current SAP record.")

	for condition in conditions:
		if not isinstance(condition, dict) or not isinstance(condition.get("when"), dict):
			raise frappe.ValidationError(
				"Each conditional_value condition requires a 'when' object."
			)
		if "value" not in condition:
			raise frappe.ValidationError("Each conditional_value condition requires a 'value'.")
		if all(_condition_matches(source.get(field), expected) for field, expected in condition["when"].items()):
			return condition["value"]

	if "default" in config:
		return config["default"]
	if config.get("strict"):
		raise frappe.ValidationError("The SAP record has no configured conditional_value equivalent.")
	return None


def _condition_matches(actual: Any, expected: Any) -> bool:
	if isinstance(expected, list):
		return actual in expected
	if not isinstance(expected, dict):
		return actual == expected
	if len(expected) != 1:
		raise frappe.ValidationError("A conditional_value operator must contain one operation.")
	operator, operand = next(iter(expected.items()))
	if operator == "in":
		return actual in operand
	if operator == "not_in":
		return actual not in operand
	if operator == "starts_with":
		prefixes = operand if isinstance(operand, list) else [operand]
		return actual is not None and str(actual).startswith(tuple(str(prefix) for prefix in prefixes))
	if operator == "not_empty":
		return (actual not in (None, "")) is bool(operand)
	raise frappe.ValidationError(f"Unsupported conditional_value operator: {operator}")


def first_value(rule: dict[str, Any], context: dict[str, Any]) -> Any:
	fields = _transform_config(rule).get("fields")
	source = context.get("source")
	if not isinstance(fields, list) or not fields or not all(isinstance(field, str) for field in fields):
		raise frappe.ValidationError("The first_value transform requires a non-empty 'fields' array.")
	if not isinstance(source, dict):
		raise frappe.ValidationError("The first_value transform requires the current SAP record.")
	return next((source.get(field) for field in fields if source.get(field) not in (None, "")), None)


def _transform_config(rule: dict[str, Any]) -> dict[str, Any]:
	config = rule.get("transform_config") or {}
	if isinstance(config, str):
		config = frappe.parse_json(config)
	if not isinstance(config, dict):
		raise frappe.ValidationError("transform_config must be a JSON object.")
	return {
		**config,
		**{key: rule[key] for key in ("doctype", "field", "key", "value") if key in rule},
	}
