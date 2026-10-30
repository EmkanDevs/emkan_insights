from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import frappe


def apply_transform(value: Any, rule: dict[str, Any], context: dict[str, Any] | None = None) -> Any:
	transform = rule.get("transform") or "none"

	if transform == "none":
		return value
	if transform == "sap_date":
		return sap_date(value)
	if transform == "sap_request_schedule_date":
		return sap_request_schedule_date(value, context or {})
	if transform == "parent_date":
		parent = (context or {}).get("parent_source") or {}
		return sap_date(parent.get(rule["fieldname"]))
	if transform == "sap_bool":
		return sap_bool(value)
	if transform == "sap_bool_not":
		return int(not sap_bool(value))
	if transform == "lookup":
		return lookup(value, rule, context or {})
	if transform == "sap_account_parent":
		return sap_account_parent(value, rule, context or {})
	if transform == "company_root_cost_center":
		return company_root_cost_center(context or {})
	if transform == "matching_selling_price_list":
		return matching_selling_price_list(rule, context or {})
	if transform == "sap_sales_tax_rows":
		return sap_sales_tax_rows(rule, context or {})
	if transform == "payment_method_account":
		return payment_method_account(rule, context or {})
	if transform == "incoming_payment_source_account":
		return incoming_payment_source_account(rule, context or {})
	if transform == "outgoing_payment_target_account":
		return outgoing_payment_target_account(rule, context or {})
	if transform == "incoming_payment_party_value":
		return incoming_payment_party_value(value, context or {})
	if transform == "incoming_payment_reference_date":
		return incoming_payment_reference_date(context or {})
	if transform == "local_payment_value":
		return local_payment_value(rule, context or {})
	if transform == "item_conversion_factor":
		return item_conversion_factor(context or {}, rule)
	if transform == "purchase_invoice_item_quantity":
		return purchase_invoice_item_quantity(value, context or {})
	if transform == "purchase_invoice_item_description":
		return purchase_invoice_item_description(value, context or {})
	if transform == "sap_item_uom":
		return sap_item_uom(value, rule, context or {})
	if transform == "sap_lookup":
		return sap_lookup(value, rule, context or {})
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
	if transform == "collection_value_map":
		return collection_value_map(rule, context or {})
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
	if transform == "journal_party":
		return journal_party(value, context or {})
	if transform == "journal_cost_center":
		return journal_cost_center(value, context or {})
	if transform == "journal_staged_voucher":
		return journal_staged_voucher(value, context or {})
	if transform == "journal_against":
		return journal_against(value, context or {})
	if transform == "staged_document":
		return staged_document(value, rule, context or {})
	if transform == "payment_ledger_amount":
		return payment_ledger_amount(rule, context or {})
	if transform == "sap_opportunity_party":
		return sap_opportunity_party(value, context or {})

	raise frappe.ValidationError(f"Unsupported SAP mapping transform: {transform}")


def sap_date(value: Any) -> str | None:
	if not value:
		return None
	return str(value)[:10]


def sap_request_schedule_date(value: Any, context: dict[str, Any]) -> str | None:
	parent = context.get("parent_source") or {}
	required_date = sap_date(value)
	posting_date = sap_date(parent.get("DocDate"))
	if required_date and posting_date and required_date >= posting_date:
		return required_date

	# ERPNext rejects required dates before posting; SAP's document due date is the source fallback.
	due_date = sap_date(parent.get("DocDueDate"))
	if due_date and posting_date and due_date >= posting_date:
		return due_date
	raise frappe.ValidationError("SAP request has no schedule date accepted by ERPNext.")


def sap_bool(value: Any) -> int:
	if isinstance(value, bool):
		return int(value)
	return int(str(value or "").strip().lower() in {"tyes", "yes", "y", "true", "1"})


def journal_party(value: Any, context: dict[str, Any]) -> str | None:
	source = context.get("source") or {}
	account_code = source.get("AccountCode")
	if not value or not account_code or str(value) == str(account_code):
		return None

	account = frappe.db.get_value(
		"Account", {"account_number": account_code, "company": context.get("company")},
		["name", "account_type"], as_dict=True,
	)
	if not account:
		raise frappe.ValidationError(f"Journal account {account_code} was not found.")
	party_type = {"Receivable": "Customer", "Payable": "Supplier"}.get(account.account_type)
	if not party_type:
		raise frappe.ValidationError(f"Journal account {account_code} has a business partner but is not receivable or payable.")
	if not frappe.db.exists(party_type, value):
		raise frappe.ValidationError(f"Journal {party_type} {value} was not found.")
	return party_type if context.get("target_field") == "party_type" else str(value)


def journal_cost_center(value: Any, context: dict[str, Any]) -> str | None:
	company = context.get("company")
	if value:
		cost_center = frappe.db.get_value("Cost Center", value, ["company", "is_group"], as_dict=True)
		if not cost_center or cost_center.company != company or cost_center.is_group:
			raise frappe.ValidationError(f"Journal cost center {value} is not a valid leaf for {company}.")
		return value

	account_code = (context.get("source") or {}).get("AccountCode")
	account = frappe.db.get_value(
		"Account", {"account_number": account_code, "company": company}, "report_type"
	)
	if account != "Profit and Loss":
		return None
	default = frappe.db.get_value("Company", company, "cost_center")
	if not default:
		raise frappe.ValidationError(f"Company {company} needs a default Cost Center for P&L journals.")
	cost_center = frappe.db.get_value("Cost Center", default, ["company", "is_group"], as_dict=True)
	if not cost_center or cost_center.company != company or cost_center.is_group:
		raise frappe.ValidationError(f"Company {company} default Cost Center is not a valid leaf.")
	return default


def journal_staged_voucher(value: Any, context: dict[str, Any]) -> str:
	company = context.get("company")
	if value in (None, "") or not company:
		raise frappe.ValidationError("SAP journal voucher needs an ID and company.")
	return journal_target(company, str(value))


@lru_cache(maxsize=4096)
def journal_target(company: str, sap_journal_id: str) -> str:
	name = frappe.db.get_value(
		"Journal Entry", {"company": company, "custom_sap_jdt_num": sap_journal_id}, "name"
	)
	if not name:
		raise frappe.ValidationError(f"SAP journal {sap_journal_id} has no ERPNext Journal Entry.")
	return name


def journal_against(value: Any, context: dict[str, Any]) -> str | None:
	return _journal_against(context.get("company"), str(value) if value else None)


@lru_cache(maxsize=4096)
def _journal_against(company: str, value: str | None) -> str | None:
	if not value:
		return value
	account = frappe.db.get_value(
		"Account", {"company": company, "account_number": value}, "name"
	)
	if account:
		return account
	if frappe.db.exists("Supplier", value) or frappe.db.exists("Customer", value):
		return value
	raise frappe.ValidationError(f"SAP journal against account or party {value} was not found.")


def staged_document(value: Any, rule: dict[str, Any], context: dict[str, Any]) -> str:
	config = _transform_config(rule)
	target = config
	if targets := config.get("targets"):
		source_type = (context.get("source") or {}).get(config.get("type_field"))
		target = targets.get(str(source_type)) if isinstance(targets, dict) else None
		if not isinstance(target, dict):
			raise frappe.ValidationError(f"SAP staged reference type {source_type} is unsupported.")

	doctype = target.get("doctype")
	configuration_name = context.get("configuration_name")
	if not doctype or value in (None, "") or not configuration_name:
		raise frappe.ValidationError("SAP staged reference needs a DocType, ID, and configuration.")
	remote_id = f"{target.get('prefix', '')}{value}"
	name = frappe.db.get_value(
		doctype,
		{"remote_id": remote_id, "external_sap_configuration": configuration_name},
		"name",
	)
	if not name:
		raise frappe.ValidationError(f"SAP reference {remote_id} is missing from {doctype} staging.")
	return name


def payment_ledger_amount(rule: dict[str, Any], context: dict[str, Any]) -> float:
	config = _transform_config(rule)
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Payment ledger amount requires a SAP source record.")

	source_kind = config.get("source_kind")
	currency_kind = config.get("currency_kind")
	if currency_kind not in {"company", "account"}:
		raise frappe.ValidationError("Payment ledger amount requires company or account currency.")

	if source_kind == "invoice":
		amount = _invoice_ledger_amount(source, currency_kind, context)
		cancelled = source.get("Cancelled")
	elif source_kind == "payment":
		amount = -_payment_unallocated_amount(source, currency_kind, context)
		cancelled = source.get("Cancelled")
	elif source_kind == "allocation":
		parent = _parent_payment_source(source)
		_validate_payment_allocations(parent, currency_kind, context)
		fieldname = _currency_field(parent, currency_kind, context, "SumApplied", "AppliedFC")
		amount = -_decimal_value(source.get(fieldname), "payment allocation")
		cancelled = parent.get("Cancelled")
	else:
		raise frappe.ValidationError(f"Unsupported payment ledger source kind: {source_kind}")

	if amount < 0 and source_kind == "invoice":
		raise frappe.ValidationError("SAP invoice total cannot be negative.")
	if sap_bool(cancelled):
		amount *= -1
	return float(amount)


def _invoice_ledger_amount(
	source: dict[str, Any], currency_kind: str, context: dict[str, Any]
) -> Decimal:
	fieldname = _currency_field(source, currency_kind, context, "DocTotal", "DocTotalFC")
	return _decimal_value(source.get(fieldname), "invoice total")


def _payment_unallocated_amount(
	source: dict[str, Any], currency_kind: str, context: dict[str, Any]
) -> Decimal:
	paid = _payment_total(source, currency_kind, context)
	applied = _payment_allocation_total(source, currency_kind, context)
	if applied - paid > Decimal("0.01"):
		raise frappe.ValidationError(
			f"SAP payment {source.get('DocEntry')} applies {applied} but payment methods total {paid}."
		)
	return max(paid - applied, Decimal(0))


def _validate_payment_allocations(
	source: dict[str, Any], currency_kind: str, context: dict[str, Any]
) -> None:
	_payment_unallocated_amount(source, currency_kind, context)


def _payment_total(
	source: dict[str, Any], currency_kind: str, context: dict[str, Any]
) -> Decimal:
	local_fields = ("TransferSum", "CashSum", "CheckSum", "CreditSum")
	foreign_fields = ("TransferSumFC", "CashSumFC", "CheckSumFC", "CreditSumFC")
	fields = local_fields
	if _currency_field(source, currency_kind, context, "local", "foreign") == "foreign":
		fields = foreign_fields
	return sum((_decimal_value(source.get(field), "payment total") for field in fields), Decimal(0))


def _payment_allocation_total(
	source: dict[str, Any], currency_kind: str, context: dict[str, Any]
) -> Decimal:
	rows = source.get("PaymentInvoices") or []
	if not isinstance(rows, list):
		raise frappe.ValidationError("SAP payment invoices must be an array.")
	fieldname = _currency_field(source, currency_kind, context, "SumApplied", "AppliedFC")
	return sum((_decimal_value(row.get(fieldname), "payment allocation") for row in rows), Decimal(0))


def _currency_field(
	source: dict[str, Any], currency_kind: str, context: dict[str, Any],
	local_field: str, foreign_field: str,
) -> str:
	if currency_kind == "company" or source.get("DocCurrency") == context.get("currency"):
		return local_field
	return foreign_field


def _parent_payment_source(source: dict[str, Any]) -> dict[str, Any]:
	parent = {
		key.removeprefix("__parent_"): value
		for key, value in source.items()
		if key.startswith("__parent_")
	}
	if not parent:
		raise frappe.ValidationError("SAP payment allocation is missing its parent payment.")
	return parent


def sap_opportunity_party(value: Any, context: dict[str, Any]) -> str:
	resolver = context.get("sap_references")
	if not value or resolver is None:
		raise frappe.ValidationError("SAP Opportunity requires a business partner reference.")
	card_type = resolver.resolve(value, {
		"entity": "BusinessPartners", "key": "CardCode", "value_field": "CardType",
	})
	party_type = {"cLid": "Lead", "cCustomer": "Customer"}.get(card_type)
	if not party_type:
		raise frappe.ValidationError(f"SAP Opportunity partner type {card_type} is unsupported.")
	if not frappe.db.exists(party_type, value):
		company = context.get("company")
		abbr = frappe.db.get_value("Company", company, "abbr") if company else None
		if not abbr or not frappe.db.exists(party_type, f"{abbr}-{value}"):
			raise frappe.ValidationError(f"ERPNext {party_type} {value} was not found.")
	return party_type if context.get("target_field") == "opportunity_from" else str(value)


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
	value_map = config.get("value_map") or {}
	if not isinstance(value_map, dict):
		raise frappe.ValidationError("The lookup transform value_map must be an object.")
	value = value_map.get(str(value), value)

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


def sap_lookup(value: Any, rule: dict[str, Any], context: dict[str, Any]) -> Any:
	config = _transform_config(rule)
	resolver = context.get("sap_references")
	if resolver is None:
		raise frappe.ValidationError("SAP lookup requires a source connection.")
	result = resolver.resolve(value, config)

	target = config.get("target")
	if target is None:
		return result
	if not isinstance(target, dict):
		raise frappe.ValidationError("SAP lookup target must be an object.")
	return lookup(result, {"transform_config": target}, context)


def collection_value_map(rule: dict[str, Any], context: dict[str, Any]) -> Any:
	config = _transform_config(rule)
	source = context.get("source")
	collection_name = config.get("collection")
	fieldname = config.get("field")
	if not isinstance(source, dict) or not collection_name or not fieldname:
		raise frappe.ValidationError(
			"Collection value mapping requires a source record, collection, and field."
		)

	rows = source.get(collection_name) or []
	if not isinstance(rows, list):
		raise frappe.ValidationError(f"SAP field '{collection_name}' must contain an array.")
	value = rows[0].get(fieldname) if rows and isinstance(rows[0], dict) else None
	mapping = config.get("mapping") or {}
	if not isinstance(mapping, dict):
		raise frappe.ValidationError("Collection value mapping requires a mapping object.")
	return mapping.get(str(value), config.get("default", value))


def sap_account_parent(value: Any, rule: dict[str, Any], context: dict[str, Any]) -> str | None:
	config = _transform_config(rule)
	source = context.get("source")
	company = context.get("company")
	if not isinstance(source, dict) or not company:
		raise frappe.ValidationError("SAP Account parent mapping requires a source record and company.")

	code = str(source.get("Code") or "")
	root_type = (config.get("root_type_by_code") or {}).get(code)
	if root_type:
		return _company_root_account(company, root_type)

	parent_code = (config.get("parent_code_overrides") or {}).get(code, value)
	if parent_code in (None, ""):
		return None

	existing = lookup(
		parent_code,
		{
			"transform_config": {
				"doctype": "Account",
				"field": "account_number",
				"company_context": True,
			}
		},
		context,
	)
	if existing:
		return existing

	resolver = context.get("sap_references")
	if resolver is None:
		raise frappe.ValidationError("SAP Account parent mapping requires a source connection.")
	parent_name = resolver.resolve(
		parent_code,
		{
			"entity": "ChartOfAccounts",
			"key": "Code",
			"value_field": "Name",
		},
	)
	return " - ".join(str(part) for part in (parent_name, parent_code) if part not in (None, ""))


def _company_root_account(company: str, root_type: str) -> str:
	roots = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"root_type": root_type,
			"parent_account": ["is", "not set"],
			"is_group": 1,
		},
		fields=["name", "account_number"],
		limit_page_length=0,
	)
	standard_roots = [root.name for root in roots if not root.account_number]
	if len(standard_roots) != 1:
		raise frappe.ValidationError(
			f"ERPNext company {company} must have one unnumbered {root_type} root Account."
		)
	return standard_roots[0]


def company_root_cost_center(context: dict[str, Any]) -> str:
	company = context.get("company")
	if not company:
		raise frappe.ValidationError("Cost Center parent mapping requires a configured company.")

	roots = frappe.get_all(
		"Cost Center",
		filters={
			"company": company,
			"cost_center_name": company,
			"parent_cost_center": ["is", "not set"],
			"is_group": 1,
		},
		pluck="name",
		limit_page_length=2,
	)
	if len(roots) != 1:
		raise frappe.ValidationError(
			f"ERPNext company {company} must have one root Cost Center named {company}."
		)
	return roots[0]


def matching_selling_price_list(rule: dict[str, Any], context: dict[str, Any]) -> str:
	config = _transform_config(rule)
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Selling price list mapping requires the SAP document.")

	customer = source.get(config.get("customer_field", "CardCode"))
	currency = source.get(config.get("currency_field", "DocCurrency"))
	if not customer or not currency:
		raise frappe.ValidationError("Selling price list mapping requires a customer and currency.")

	default_price_list = frappe.db.get_value("Customer", customer, "default_price_list")
	if default_price_list and frappe.db.exists(
		"Price List",
		{"name": default_price_list, "currency": currency, "selling": 1, "enabled": 1},
	):
		return default_price_list

	price_lists = frappe.get_all(
		"Price List",
		filters={"currency": currency, "selling": 1, "enabled": 1},
		pluck="name",
		limit_page_length=2,
	)
	if not price_lists:
		price_lists = frappe.get_all(
			"Price List",
			filters={"currency": currency, "enabled": 1},
			pluck="name",
			limit_page_length=2,
		)
	if len(price_lists) != 1:
		raise frappe.ValidationError(
			f"ERPNext must have one enabled Price List in {currency} for customer {customer}."
		)
	return price_lists[0]


def sap_sales_tax_rows(rule: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
	config = _transform_config(rule)
	source = context.get("source")
	company = context.get("company")
	if not isinstance(source, dict) or not company:
		raise frappe.ValidationError("SAP sales tax mapping requires the document and company.")

	lines = source.get(config.get("collection", "DocumentLines")) or []
	if not isinstance(lines, list):
		raise frappe.ValidationError("SAP sales tax lines must be an array.")

	rate_accounts = config.get("rate_accounts") or {}
	if not isinstance(rate_accounts, dict):
		raise frappe.ValidationError("SAP sales tax rate_accounts must be an object.")
	totals: dict[str, Decimal] = {}
	for line in lines:
		if not isinstance(line, dict):
			raise frappe.ValidationError("SAP sales tax lines must contain objects.")
		amount = _decimal_value(line.get(config.get("amount_field", "TaxTotal")), "tax amount")
		if not amount:
			continue
		rate = _decimal_value(line.get(config.get("rate_field", "TaxPercentagePerRow")), "tax rate")
		rate_key = format(rate.normalize(), "f")
		totals[rate_key] = totals.get(rate_key, Decimal(0)) + amount

	header_total = _decimal_value(source.get(config.get("total_field", "VatSum")), "tax total")
	if abs(sum(totals.values(), Decimal(0)) - header_total) > Decimal("0.01"):
		raise frappe.ValidationError("SAP sales tax line amounts do not match the document tax total.")

	rows = []
	for rate_key, amount in totals.items():
		account_number = rate_accounts.get(rate_key)
		if not account_number:
			raise frappe.ValidationError(f"No ERPNext output VAT account is mapped for rate {rate_key}%.")
		account = lookup(
			account_number,
			{
				"transform_config": {
					"doctype": "Account",
					"field": "account_number",
					"company_context": True,
					"required": True,
				}
			},
			context,
		)
		rows.append(
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": f"Output VAT {rate_key}%",
				"tax_amount": float(amount),
			}
		)
	return rows


def _decimal_value(value: Any, label: str) -> Decimal:
	try:
		if isinstance(value, bool):
			raise InvalidOperation
		return Decimal(str(value or 0))
	except (InvalidOperation, ValueError) as exc:
		raise frappe.ValidationError(f"SAP {label} must be numeric.") from exc


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


def incoming_payment_source_account(rule: dict[str, Any], context: dict[str, Any]) -> str:
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Incoming payment source account needs the SAP payment record.")
	code = _incoming_payment_source_account_code(source)
	return_field = _transform_config(rule).get("return_field", "name")
	if return_field not in {"name", "account_currency"}:
		raise frappe.ValidationError("Incoming payment source account needs a valid return field.")
	return lookup(code, {
		"transform_config": {
			"doctype": "Account", "field": "account_number",
			"return_field": return_field, "company_context": True, "required": True,
		}
	}, context)


def outgoing_payment_target_account(rule: dict[str, Any], context: dict[str, Any]) -> str:
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Outgoing payment target account needs the SAP payment record.")
	code = _outgoing_payment_target_account_code(source)
	return_field = _transform_config(rule).get("return_field", "name")
	if return_field not in {"name", "account_currency"}:
		raise frappe.ValidationError("Outgoing payment target account needs a valid return field.")
	return lookup(code, {
		"transform_config": {
			"doctype": "Account", "field": "account_number",
			"return_field": return_field, "company_context": True, "required": True,
		}
	}, context)


def incoming_payment_party_value(value: Any, context: dict[str, Any]) -> Any:
	source = context.get("source") or {}
	if source.get("DocType") == "rAccount":
		return None
	if source.get("DocType") in {"rCustomer", "rSupplier"}:
		return value
	raise frappe.ValidationError("SAP incoming payment has an unsupported document type.")


def incoming_payment_reference_date(context: dict[str, Any]) -> str | None:
	source = context.get("source") or {}
	return sap_date(source.get("TransferDate") or source.get("DocDate"))


def _incoming_payment_source_account_code(source: dict[str, Any]) -> str:
	source_type = source.get("DocType")
	if source_type == "rAccount":
		code = source.get("CardCode")
	elif source_type in {"rCustomer", "rSupplier"}:
		code = source.get("ControlAccount")
	else:
		raise frappe.ValidationError("SAP incoming payment has an unsupported document type.")
	if not code:
		raise frappe.ValidationError("SAP incoming payment source account code is missing.")
	return str(code)


def _outgoing_payment_target_account_code(source: dict[str, Any]) -> str:
	source_type = source.get("DocType")
	if source_type == "rAccount":
		code = source.get("CardCode")
	elif source_type == "rSupplier":
		code = source.get("ControlAccount")
	else:
		raise frappe.ValidationError("SAP outgoing payment has an unsupported document type.")
	if not code:
		raise frappe.ValidationError("SAP outgoing payment target account code is missing.")
	return str(code)


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
	account_code = (
		_incoming_payment_source_account_code(source)
		if _transform_config(rule).get("incoming") else _outgoing_payment_target_account_code(source)
	)
	party_currency = lookup(account_code, {
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


def sap_item_uom(value: Any, rule: dict[str, Any], context: dict[str, Any]) -> str | None:
	config = _transform_config(rule)
	if not value and config.get("service_uom_fallback") and (
		(context.get("parent_source") or {}).get("DocType") == "dDocument_Service"
	):
		return "Nos"
	if value != "Manual" or not config.get("manual_uom_fallback"):
		return value

	source = context.get("source") or {}
	item_code = source.get("ItemCode")
	if _decimal_value(source.get("UnitsOfMeasurment"), "SAP manual UOM factor") != 1:
		raise frappe.ValidationError("SAP manual UOM requires a one-to-one item conversion.")
	uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None
	if not uom:
		raise frappe.ValidationError("ERP item stock UOM is missing for the SAP manual UOM.")
	return uom


def item_conversion_factor(context: dict[str, Any], rule: dict[str, Any] | None = None) -> float:
	source = context.get("source")
	if not isinstance(source, dict):
		raise frappe.ValidationError("Item conversion needs the SAP document line.")
	if not source.get("ItemCode") and (
		(context.get("parent_source") or {}).get("DocType") == "dDocument_Service"
	):
		return 1.0
	item_code = source.get("ItemCode")
	uom = sap_item_uom(source.get("UoMCode"), rule or {}, context)
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


def purchase_invoice_item_quantity(value: Any, context: dict[str, Any]) -> float:
	if (context.get("parent_source") or {}).get("DocType") == "dDocument_Service" and not value:
		return 1.0
	return value


def purchase_invoice_item_description(value: Any, context: dict[str, Any]) -> str | None:
	if value:
		return value
	if (context.get("parent_source") or {}).get("DocType") != "dDocument_Service":
		return value
	source = context.get("source") or {}
	account_code = source.get("AccountCode")
	company = context.get("company")
	if not account_code or not company:
		raise frappe.ValidationError("SAP service invoice line needs an expense account.")
	account_name = frappe.db.get_value(
		"Account", {"account_number": account_code, "company": company}, "account_name"
	)
	if not account_name:
		raise frappe.ValidationError("ERPNext service expense Account was not found.")
	return account_name


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
	config = _transform_config(rule)
	fields = config.get("fields")
	source = context.get("source")
	if not isinstance(fields, list) or not fields or not all(isinstance(field, str) for field in fields):
		raise frappe.ValidationError("The first_value transform requires a non-empty 'fields' array.")
	if not isinstance(source, dict):
		raise frappe.ValidationError("The first_value transform requires the current SAP record.")
	value = next((source.get(field) for field in fields if source.get(field) not in (None, "")), None)
	return truncate(value, rule) if "max_length" in config else value


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
