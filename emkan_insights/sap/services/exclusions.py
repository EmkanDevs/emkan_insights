import re

import frappe


def get_excluded_sap_id_prefixes(mapping_doc) -> tuple[str, ...]:
	value = getattr(mapping_doc, "excluded_sap_id_prefixes", None) or ""
	prefixes = tuple(prefix.strip() for prefix in value.split(",") if prefix.strip())
	if any(not re.fullmatch(r"[A-Za-z0-9]+", prefix) for prefix in prefixes):
		raise frappe.ValidationError("Excluded SAP ID prefixes must contain only letters and digits.")
	if len(prefixes) != len(set(prefixes)):
		raise frappe.ValidationError("Excluded SAP ID prefixes must be unique.")
	return prefixes


def is_excluded_sap_id(raw_id, prefixes: tuple[str, ...]) -> bool:
	return raw_id not in (None, "") and str(raw_id).startswith(prefixes)


def allowed_staged_id_condition(remote_id, sap_id_prefix: str, prefixes: tuple[str, ...]):
	condition = remote_id.like(f"{sap_id_prefix}%") if sap_id_prefix else remote_id != ""
	for prefix in prefixes:
		condition &= ~remote_id.like(f"{sap_id_prefix}{prefix}%")
	return condition
