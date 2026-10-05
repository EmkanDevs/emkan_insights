from __future__ import annotations

import re

import frappe


class SAPReferenceResolver:
	def __init__(self, client):
		self.client = client
		self.cache = {}

	def resolve(self, value, config):
		if value in (None, "") or value in config.get("empty_values", []):
			return None
		entity, key, field = self._identifiers(config)
		literal = self._literal(value, config.get("key_type", "string"))
		cache_key = (entity, key, field, literal)
		if cache_key not in self.cache:
			payload = self.client.get(
				entity,
				params={"$filter": f"{key} eq {literal}", "$select": field, "$top": 2},
			)
			rows = payload.get("value")
			if not isinstance(rows, list) or len(rows) != 1 or field not in rows[0]:
				raise frappe.ValidationError(f"SAP reference {entity}.{key} did not resolve uniquely.")
			self.cache[cache_key] = rows[0][field]
		return self.cache[cache_key]

	def exists(self, value, config) -> int:
		if value in (None, "") or value in config.get("empty_values", []):
			return 0
		entity, key = self._entity_key(config)
		literal = self._literal(value, config.get("key_type", "string"))
		extra_filter = config.get("filter")
		if extra_filter is not None and not isinstance(extra_filter, str):
			raise frappe.ValidationError("SAP exists filter must be a string.")
		filter_expression = f"{key} eq {literal}"
		if extra_filter:
			filter_expression = f"({filter_expression}) and ({extra_filter.strip()})"
		cache_key = ("exists", entity, key, literal, extra_filter)
		if cache_key not in self.cache:
			payload = self.client.get(
				entity,
				params={"$filter": filter_expression, "$select": key, "$top": 1},
			)
			rows = payload.get("value")
			if not isinstance(rows, list):
				raise frappe.ValidationError(f"SAP reference {entity}.{key} returned invalid data.")
			self.cache[cache_key] = int(bool(rows))
		return self.cache[cache_key]

	@staticmethod
	def _identifiers(config):
		identifiers = tuple(config.get(key) for key in ("entity", "key", "value_field"))
		if not all(
			isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
			for value in identifiers
		):
			raise frappe.ValidationError("SAP lookup requires entity, key, and value_field identifiers.")
		return identifiers

	@staticmethod
	def _entity_key(config):
		identifiers = tuple(config.get(key) for key in ("entity", "key"))
		if not all(
			isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
			for value in identifiers
		):
			raise frappe.ValidationError("SAP exists requires entity and key identifiers.")
		return identifiers

	@staticmethod
	def _literal(value, key_type):
		if key_type == "integer":
			if not re.fullmatch(r"-?\d+", str(value)):
				raise frappe.ValidationError("SAP lookup requires an integer key.")
			return str(int(value))
		if key_type != "string":
			raise frappe.ValidationError("SAP lookup key_type must be string or integer.")
		return "'" + str(value).replace("'", "''") + "'"
