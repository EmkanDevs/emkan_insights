from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe


def apply_transform(value: Any, rule: dict[str, Any], context: dict[str, Any] | None = None) -> Any:
	transform = rule.get("transform") or "none"

	if transform == "none":
		return value
	if transform == "sap_date":
		return sap_date(value)
	if transform == "parent_date":
		parent = (context or {}).get("parent_source") or {}
		return sap_date(parent.get(rule["fieldname"]))
	if transform == "sap_bool":
		return sap_bool(value)
	if transform == "sap_bool_not":
		return int(not sap_bool(value))
	if transform == "lookup":
		return lookup(value, rule, context or {})
	if transform == "payment_method_account":
		return payment_method_account(rule, context or {})
	if transform == "local_payment_value":
		return local_payment_value(rule, context or {})
	if transform == "item_conversion_factor":
		return item_conversion_factor(context or {})
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
	if transform == "sum_fields":
		return sum_fields(rule, context or {})
	if transform == "join_fields":
		return join_fields(rule, context or {})
	if transform == "truncate":
		return truncate(value, rule)
	if transform == "multiply":
		return value * _transform_config(rule).get("factor", 1) if value is not None else None
	if transform == "sap_account_amount":
		source = (context or {}).get("source") or {}
		return value if source.get("FCCurrency") else source.get(_transform_config(rule)["base_field"])
	if transform == "sap_account_currency":
		return value or (context or {}).get("currency")

	raise frappe.ValidationError(f"Unsupported SAP mapping transform: {transform}")


def sap_date(value: Any) -> str | None:
	if not value:
		return None
	return str(value)[:10]


def sap_bool(value: Any) -> int:
	if isinstance(value, bool):
		return int(value)
	return int(str(value or "").strip().lower() in {"tyes", "yes", "y", "true", "1"})


def truncate(value: Any, rule: dict[str, Any]) -> Any:
	max_length = validate_truncate_config(rule)
	return value[:max_length] if isinstance(value, str) else value


def validate_truncate_config(rule: dict[str, Any]) -> int:
	max_length = _transform_config(rule).get("max_length")
	if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
		raise frappe.ValidationError("The truncate transform requires a positive integer 'max_length'.")
	return max_length


def lookup(value: Any, rule: dict[str, Any], context: dict[str, Any] | None = None) -> str | None:
	if value in (None, ""):
		return None

	config = _transform_config(rule)
	doctype = config.get("doctype")
	fieldname = config.get("field")
	if not doctype or not fieldname:
		raise frappe.ValidationError("The lookup transform requires 'doctype' and 'field'.")

	filters = {fieldname: value}
	if config.get("company_context"):
		company = (context or {}).get("company")
		if not company:
			raise frappe.ValidationError("Account lookup requires a configured company.")
		filters["company"] = company
	return_field = config.get("return_field", "name")
	result = frappe.db.get_value(doctype, filters, return_field)
	if result is None and config.get("required"):
		raise frappe.ValidationError(f"ERPNext reference {doctype}.{fieldname} was not found.")
	return result


def payment_method_account(rule: dict[str, Any], context: dict[str, Any]) -> str:
	config = _transform_config(rule)
	return_field = config.get("return_field", "name")
	if return_field not in {"name", "account_currency"}:
		raise frappe.ValidationError("Payment method account needs a valid return field.")
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Payment method account needs the SAP payment record.")
	account_code = _payment_method_account_code(source)
	return lookup(account_code, {
		"transform_config": {
			"doctype": "Account", "field": "account_number",
			"return_field": return_field, "company_context": True, "required": True,
		}
	}, context)


def _payment_method_account_code(source: dict[str, Any]) -> str:
	methods = (
		("TransferSum", "TransferAccount"),
		("CashSum", "CashAccount"),
		("CheckSum", None),
		("CreditSum", None),
	)
	try:
		active = [account_field for amount_field, account_field in methods
			if Decimal(str(source.get(amount_field) or 0)) > 0]
	except InvalidOperation as exc:
		raise frappe.ValidationError("SAP payment method amounts must contain numbers.") from exc
	if len(active) != 1 or not active[0]:
		raise frappe.ValidationError("SAP payment needs one supported cash or transfer method.")
	account_code = source.get(active[0])
	if not account_code:
		raise frappe.ValidationError("SAP payment method account code is missing.")
	return account_code


def local_payment_value(rule: dict[str, Any], context: dict[str, Any]) -> float | int:
	kind = _transform_config(rule).get("kind")
	if kind not in {"rate", "amount"}:
		raise frappe.ValidationError("Local payment value needs rate or amount kind.")
	source = context.get("source")
	currency = context.get("currency")
	if not isinstance(source, dict) or not currency or source.get("DocCurrency") != currency:
		raise frappe.ValidationError("SAP payment needs a verified foreign currency mapping.")
	party_currency = lookup(source.get("ControlAccount"), {
		"transform_config": {
			"doctype": "Account", "field": "account_number",
			"return_field": "account_currency", "company_context": True, "required": True,
		}
	}, context)
	method_currency = payment_method_account({
		"transform_config": {"return_field": "account_currency"}
	}, context)
	if party_currency != currency or method_currency != currency:
		raise frappe.ValidationError("SAP payment account currencies need a verified exchange rate.")
	if kind == "rate":
		return 1
	amount = sum_fields({
		"transform_config": {"fields": ["TransferSum", "CashSum", "CheckSum", "CreditSum"]}
	}, context)
	if amount is None or amount <= 0:
		raise frappe.ValidationError("SAP payment amount must be positive.")
	return amount


def item_conversion_factor(context: dict[str, Any]) -> float:
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Item conversion needs the SAP document line.")
	item_code = source.get("ItemCode")
	uom = source.get("UoMCode")
	if not item_code or not uom:
		raise frappe.ValidationError("SAP item line needs an item code and UOM.")
	factor = frappe.db.get_value(
		"UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor"
	)
	if factor and float(factor) > 0:
		return float(factor)
	if frappe.db.get_value("Item", item_code, "stock_uom") == uom:
		return 1.0
	raise frappe.ValidationError("ERP item UOM conversion is missing for the SAP line.")


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
	key = str(value)
	if config.get("normalize") == "lower":
		key = key.strip().lower()
		mapping = {str(source).strip().lower(): target for source, target in mapping.items()}
	if config.get("strict") and value not in (None, "") and key not in mapping:
		raise frappe.ValidationError("The SAP value has no configured ERPNext equivalent.")
	return mapping.get(key, config.get("default", value))


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
		fields = sorted({field for condition in conditions for field in condition["when"]})
		values = ", ".join(f"{field}={source.get(field)!r}" for field in fields)
		target = context.get("target_field") or "unknown"
		raise frappe.ValidationError(
			f"No conditional_value match for ERPNext field '{target}' with SAP values: {values}."
		)
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


def sum_fields(rule: dict[str, Any], context: dict[str, Any]) -> float | None:
	fields = validate_sum_fields_config(rule)
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("The sum_fields transform requires the current SAP record.")
	values = [source[field] for field in fields if source.get(field) not in (None, "")]
	if not values:
		return None
	try:
		if any(isinstance(value, bool) for value in values):
			raise ValueError
		return float(sum(Decimal(str(value)) for value in values))
	except (InvalidOperation, ValueError) as exc:
		raise frappe.ValidationError("SAP amount fields must contain numbers.") from exc


def validate_sum_fields_config(rule: dict[str, Any]) -> list[str]:
	fields = _transform_config(rule).get("fields")
	if not isinstance(fields, list) or not fields or not all(isinstance(field, str) and field for field in fields):
		raise frappe.ValidationError("The sum_fields transform requires source field names.")
	return fields


def join_fields(rule: dict[str, Any], context: dict[str, Any]) -> str:
	config = validate_join_fields_config(rule)
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("The join_fields transform requires the current SAP record.")
	fields = config["fields"]
	missing = [
		field for field in fields
		if source.get(field) is None or str(source[field]).strip() == ""
	]
	if missing and not config.get("skip_missing"):
		target = context.get("target_field") or "unknown"
		raise frappe.ValidationError(
			f"Cannot build ERPNext field '{target}': missing SAP fields {', '.join(missing)}."
		)
	values = [str(source[field]).strip() for field in fields if field not in missing]
	if not values:
		raise frappe.ValidationError("The join_fields transform needs at least one source value.")
	return config.get("separator", " ").join(values)


def validate_join_fields_config(rule: dict[str, Any]) -> dict[str, Any]:
	config = _transform_config(rule)
	fields = config.get("fields")
	if (
		not isinstance(fields, list)
		or len(fields) < 2
		or not all(isinstance(field, str) and field for field in fields)
		or not isinstance(config.get("separator", " "), str)
		or not isinstance(config.get("skip_missing", False), bool)
	):
		raise frappe.ValidationError("The join_fields transform requires fields and a string separator.")
	return config


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
