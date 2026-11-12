import frappe

SMALL_ENTITIES = {
	"FAAccountDeterminations", "Banks", "Manufacturers", "UnitOfMeasurements",
	"BusinessPartnerGroups", "SalesOpportunitySourcesSetup", "SalesStages",
}


def _mark(node) -> int:
	count = 0
	if isinstance(node, dict):
		config = node.get("transform_config")
		if (
			node.get("transform") == "sap_lookup" and isinstance(config, dict)
			and config.get("entity") in SMALL_ENTITIES and not config.get("preload")
		):
			config["preload"] = True
			count += 1
		for value in node.values():
			count += _mark(value)
	elif isinstance(node, list):
		for value in node:
			count += _mark(value)
	return count


def execute():
	"""Fetch small SAP lookup tables once per run instead of once per record."""
	if not frappe.db.table_exists("SAP Doctype Mapping"):
		return
	for name, field_mapping in frappe.db.get_all(
		"SAP Doctype Mapping", fields=["name", "field_mapping"], as_list=True
	):
		mapping = frappe.parse_json(field_mapping)
		if _mark(mapping):
			frappe.db.set_value(
				"SAP Doctype Mapping", name, "field_mapping",
				frappe.as_json(mapping, indent=2), update_modified=False,
			)
