import frappe


def parse_field_rules(raw, doctype):
	if not raw:
		return {}
	try:
		rules = frappe.parse_json(raw) if isinstance(raw, str) else raw
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"Import field rules for {doctype} must be valid JSON.") from exc
	if not isinstance(rules, dict):
		raise frappe.ValidationError(f"Import field rules for {doctype} must be an object.")

	meta = frappe.get_meta(doctype)
	for target, rule in rules.items():
		field = meta.get_field(target)
		if not field or field.fieldtype in {"Table", "Table MultiSelect"}:
			raise frappe.ValidationError(f"Import rule target {doctype}.{target} must be a stored field.")
		if not isinstance(rule, dict) or set(rule) - {"default", "join", "separator"}:
			raise frappe.ValidationError(f"Invalid import rule for {doctype}.{target}.")
		if ("default" in rule) == ("join" in rule):
			raise frappe.ValidationError(f"Import rule for {doctype}.{target} requires default or join.")
		if "join" in rule:
			if field.fieldtype not in {"Data", "Small Text", "Text", "Long Text"}:
				raise frappe.ValidationError(f"Join target {doctype}.{target} must be a text field.")
			fields = rule["join"]
			if (not isinstance(fields, list) or len(fields) < 2
				or not all(isinstance(name, str) and meta.get_field(name) for name in fields)
				or not isinstance(rule.get("separator", " "), str)):
				raise frappe.ValidationError(f"Invalid join fields for {doctype}.{target}.")
		elif "separator" in rule:
			raise frappe.ValidationError(f"Separator requires join for {doctype}.{target}.")
		elif rule["default"] is None or isinstance(rule["default"], (dict, list)):
			raise frappe.ValidationError(f"Default for {doctype}.{target} must be a scalar value.")
	return rules


def apply_field_rules(row, rules):
	result = dict(row)
	for target, rule in rules.items():
		if result.get(target) not in (None, ""):
			continue
		if "default" in rule:
			result[target] = rule["default"]
			continue
		parts = [result.get(field) for field in rule["join"]]
		if all(part not in (None, "") for part in parts):
			result[target] = rule.get("separator", " ").join(str(part) for part in parts)
	return result
