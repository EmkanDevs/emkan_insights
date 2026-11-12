from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import frappe


class SAPReferenceResolver:
	PURCHASE_REQUEST_TYPE = 1470000113
	PURCHASE_QUOTATION_TYPE = 540000006
	BATCH_ID_CHUNK = 20
	EXISTS_PRELOAD_AFTER = 10
	# Batching only fills the cache sooner; set False to force one request per lookup.
	batching_enabled = True

	def __init__(self, client):
		self.client = client
		self.cache = {}
		self._exists_misses = {}

	def resolve(self, value, config):
		if value in (None, "") or value in config.get("empty_values", []):
			return None
		entity, key, field = self._identifiers(config)
		literal = self._literal(value, config.get("key_type", "string"))
		cache_key = (entity, key, field, literal)
		if config.get("preload") and cache_key not in self.cache:
			rows = self._preloaded(entity, key)
			matches = rows.get(self._index_value(value, config.get("key_type", "string")), [])
			if len(matches) != 1 or field not in matches[0]:
				raise frappe.ValidationError(f"SAP reference {entity}.{key} did not resolve uniquely.")
			self.cache[cache_key] = matches[0][field]
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

	def _preloaded(self, entity: str, key: str) -> dict[str, list[dict]]:
		"""Fetch a small lookup entity once per run instead of one request per record."""
		cache_key = ("preload", entity, key)
		if cache_key not in self.cache:
			rows: dict[str, list[dict]] = {}
			for row in self.client.fetch_all(entity):
				if row.get(key) not in (None, ""):
					rows.setdefault(str(row[key]), []).append(row)
			self.cache[cache_key] = rows
		return self.cache[cache_key]

	@staticmethod
	def _index_value(value, key_type) -> str:
		return str(int(value)) if key_type == "integer" and re.fullmatch(r"-?\d+", str(value)) else str(value)

	def purchase_request_line(self, source_line: dict) -> tuple[str, int] | None:
		key = (
			"purchase_request_line", source_line.get("BaseType"),
			source_line.get("BaseEntry"), source_line.get("BaseLine"), source_line.get("ItemCode"),
		)
		if key not in self.cache:
			self.cache[key] = self._purchase_request_line(source_line)
		return self.cache[key]

	def vendor_payment_from_advice(self, journal_id, card_code) -> str:
		journal_reference = self.resolve(journal_id, {
			"entity": "JournalEntries", "key": "JdtNum",
			"value_field": "Reference", "key_type": "integer",
		})
		payment_id = self.resolve(journal_reference, {
			"entity": "VendorPayments", "key": "DocNum",
			"value_field": "DocEntry", "key_type": "integer",
		})
		payment_party = self.resolve(journal_reference, {
			"entity": "VendorPayments", "key": "DocNum",
			"value_field": "CardCode", "key_type": "integer",
		})
		if not card_code or payment_party != card_code:
			raise frappe.ValidationError(
			f"SAP payment advice {journal_id} belongs to a different supplier."
		)
		return str(payment_id)

	def verify_single_invoice_payment(self, payment, invoice_row, reported_amount, paid_amount) -> None:
		payment_id = payment.get("DocEntry")
		invoice_id = invoice_row.get("DocEntry")
		card_code = payment.get("CardCode")
		date = str(payment.get("DocDate") or "")[:10]
		if not all((payment_id is not None, invoice_id is not None, card_code, date, payment.get("DocNum"))):
			raise frappe.ValidationError("SAP overapplied payment lacks a document, party, or date.")
		key = ("single_invoice_payment", payment.get("DocType"), str(payment_id), str(invoice_id), str(paid_amount))
		if key in self.cache:
			return
		outgoing = payment.get("DocType") == "rSupplier"
		invoice_entity = "PurchaseInvoices" if outgoing else "Invoices"

		invoice_total = self.resolve(invoice_id, {
			"entity": invoice_entity, "key": "DocEntry", "value_field": "DocTotal",
			"key_type": "integer",
		})
		invoice_party = self.resolve(invoice_id, {
			"entity": invoice_entity, "key": "DocEntry", "value_field": "CardCode",
			"key_type": "integer",
		})
		try:
			total = Decimal(str(invoice_total))
		except (InvalidOperation, ValueError) as exc:
			raise frappe.ValidationError("SAP invoice total is not numeric.") from exc
		if invoice_party != card_code or total != reported_amount:
			raise frappe.ValidationError(
			f"SAP payment {payment_id} overapplied invoice amount differs from its invoice."
		)

		reference = self._literal(payment["DocNum"], "string")
		journals = self.client.fetch_all("JournalEntries", params={
			"$filter": f"Reference eq {reference}",
			"$select": "JdtNum,ReferenceDate,Memo,JournalEntryLines",
		})
		matches = []
		for journal in journals:
			if str(journal.get("ReferenceDate") or "")[:10] != date:
				continue
			payment_type = "Outgoing" if outgoing else "Incoming"
			if not str(journal.get("Memo") or "").startswith(f"{payment_type} Payments - {card_code}"):
				continue
			lines = [line for line in journal.get("JournalEntryLines") or [] if line.get("ShortName") == card_code]
			credit = sum((Decimal(str(line.get("Credit") or 0)) for line in lines), Decimal(0))
			debit = sum((Decimal(str(line.get("Debit") or 0)) for line in lines), Decimal(0))
			posted_amount = debit if outgoing else credit
			opposite_amount = credit if outgoing else debit
			if posted_amount == paid_amount and opposite_amount == 0:
				matches.append(journal)
		if len(matches) != 1:
			raise frappe.ValidationError(
				f"SAP payment {payment_id} needs one matching journal for its invoice allocation."
			)
		self.cache[key] = True

	def verify_outgoing_payment_total(self, payment, paid_amount) -> None:
		payment_id = payment.get("DocEntry")
		card_code = payment.get("CardCode")
		date = str(payment.get("DocDate") or "")[:10]
		if payment.get("DocType") != "rSupplier" or not all((payment_id, payment.get("DocNum"), card_code, date)):
			raise frappe.ValidationError("SAP outgoing payment lacks a document, supplier, or date.")
		key = ("outgoing_payment_total", str(payment_id), str(paid_amount))
		if key in self.cache:
			return

		reference = self._literal(payment["DocNum"], "string")
		journals = self.client.fetch_all("JournalEntries", params={
			"$filter": f"Reference eq {reference}",
			"$select": "JdtNum,ReferenceDate,Memo,JournalEntryLines",
		})
		matches = []
		for journal in journals:
			if str(journal.get("ReferenceDate") or "")[:10] != date:
				continue
			if journal.get("Memo") != f"Outgoing Payments - {card_code}":
				continue
			lines = [line for line in journal.get("JournalEntryLines") or [] if line.get("ShortName") == card_code]
			debit = sum((Decimal(str(line.get("Debit") or 0)) for line in lines), Decimal(0))
			credit = sum((Decimal(str(line.get("Credit") or 0)) for line in lines), Decimal(0))
			if debit == paid_amount and credit == 0:
				matches.append(journal)
		if len(matches) != 1:
			raise frappe.ValidationError(f"SAP payment {payment_id} needs one matching posted supplier debit.")
		self.cache[key] = True

	def purchase_credit_note_journal(self, credit_note_id, card_code, applied_amount) -> str:
		if credit_note_id is None or not card_code or applied_amount <= 0:
			raise frappe.ValidationError("SAP purchase credit note needs an ID, supplier, and positive amount.")
		key = ("purchase_credit_note_journal", str(credit_note_id), card_code, str(applied_amount))
		if key in self.cache:
			return self.cache[key]

		credit_note = self.client.get("PurchaseCreditNotes", params={
			"$filter": f"DocEntry eq {self._literal(credit_note_id, 'integer')}",
			"$select": "DocEntry,CardCode,DocTotal,TransNum,DocDate,JournalMemo", "$top": 2,
		}).get("value")
		if not isinstance(credit_note, list) or len(credit_note) != 1:
			raise frappe.ValidationError(f"SAP purchase credit note {credit_note_id} did not resolve uniquely.")
		credit_note = credit_note[0]
		journal_id = credit_note.get("TransNum")
		if (
			credit_note.get("CardCode") != card_code
			or Decimal(str(credit_note.get("DocTotal") or 0)) != applied_amount
			or not journal_id
		):
			raise frappe.ValidationError(f"SAP purchase credit note {credit_note_id} differs from its payment.")
		journal = self.client.get("JournalEntries", params={
			"$filter": f"JdtNum eq {self._literal(journal_id, 'integer')}",
			"$select": "JdtNum,ReferenceDate,Memo,JournalEntryLines", "$top": 2,
		}).get("value")
		if not isinstance(journal, list) or len(journal) != 1:
			raise frappe.ValidationError(f"SAP credit note {credit_note_id} journal did not resolve uniquely.")
		journal = journal[0]
		if (
			str(journal.get("ReferenceDate") or "")[:10] != str(credit_note.get("DocDate") or "")[:10]
			or journal.get("Memo") != credit_note.get("JournalMemo")
		):
			raise frappe.ValidationError(f"SAP credit note {credit_note_id} journal date or memo differs.")
		lines = [line for line in journal.get("JournalEntryLines") or [] if line.get("ShortName") == card_code]
		debit = sum((Decimal(str(line.get("Debit") or 0)) for line in lines), Decimal(0))
		credit = sum((Decimal(str(line.get("Credit") or 0)) for line in lines), Decimal(0))
		if debit != applied_amount or credit != 0:
			raise frappe.ValidationError(f"SAP credit note {credit_note_id} does not reduce supplier payable.")
		self.cache[key] = str(journal_id)
		return self.cache[key]

	def negative_purchase_invoice_sign(self, invoice) -> int:
		invoice_id = invoice.get("DocEntry")
		journal_id = invoice.get("TransNum")
		card_code = invoice.get("CardCode")
		date = str(invoice.get("DocDate") or "")[:10]
		try:
			total = Decimal(str(invoice.get("DocTotal")))
		except (InvalidOperation, TypeError, ValueError) as exc:
			raise frappe.ValidationError("SAP negative purchase invoice total is not numeric.") from exc
		if not all((invoice_id is not None, journal_id, card_code, date)) or total >= 0:
			raise frappe.ValidationError("SAP negative purchase invoice lacks a posted journal or party.")
		key = ("negative_purchase_invoice", str(invoice_id), str(journal_id), str(total))
		if key in self.cache:
			return self.cache[key]

		journal = self.client.get("JournalEntries", params={
			"$filter": f"JdtNum eq {self._literal(journal_id, 'integer')}",
			"$select": "JdtNum,ReferenceDate,Memo,JournalEntryLines", "$top": 2,
		}).get("value")
		if not isinstance(journal, list) or len(journal) != 1:
			raise frappe.ValidationError(f"SAP invoice {invoice_id} journal did not resolve uniquely.")
		journal = journal[0]
		if (
			str(journal.get("ReferenceDate") or "")[:10] != date
			or journal.get("Memo") != invoice.get("JournalMemo")
			or not str(journal.get("Memo") or "").endswith(f" - {card_code}")
		):
			raise frappe.ValidationError(f"SAP invoice {invoice_id} journal date or party differs.")
		lines = [line for line in journal.get("JournalEntryLines") or [] if line.get("ShortName") == card_code]
		debit = sum((Decimal(str(line.get("Debit") or 0)) for line in lines), Decimal(0))
		credit = sum((Decimal(str(line.get("Credit") or 0)) for line in lines), Decimal(0))
		if debit == -total and credit == 0:
			sign = -1
		elif credit == -total and debit == 0:
			sign = 1
		else:
			raise frappe.ValidationError(f"SAP invoice {invoice_id} journal does not match its negative total.")
		self.cache[key] = sign
		return sign

	def purchase_receipt_line(self, source_line: dict) -> tuple[str, int]:
		receipt_id = source_line.get("BaseEntry")
		line_number = source_line.get("BaseLine")
		if any(isinstance(value, bool) or not str(value).isdigit() for value in (receipt_id, line_number)):
			raise frappe.ValidationError("SAP Purchase Invoice receipt reference has an invalid entry or line.")
		receipt_id = str(receipt_id)
		receipt_lines = self.resolve(receipt_id, {
			"entity": "PurchaseDeliveryNotes", "key": "DocEntry",
			"value_field": "DocumentLines", "key_type": "integer",
		})
		index, receipt_line = self._document_line(receipt_lines, int(line_number))
		if receipt_line.get("ItemCode") != source_line.get("ItemCode"):
			raise frappe.ValidationError(
				f"SAP Purchase Invoice item differs from receipt {receipt_id} line {line_number}."
			)
		return receipt_id, index + 1

	def _purchase_request_line(self, source_line: dict) -> tuple[str, int] | None:
		base_type = source_line.get("BaseType")
		if base_type == self.PURCHASE_QUOTATION_TYPE:
			quote_lines = self.resolve(source_line.get("BaseEntry"), {
				"entity": "PurchaseQuotations", "key": "DocEntry",
				"value_field": "DocumentLines", "key_type": "integer",
			})
			_, quote_line = self._document_line(quote_lines, source_line.get("BaseLine"))
			if quote_line.get("ItemCode") != source_line.get("ItemCode"):
				raise frappe.ValidationError("SAP Purchase Order item differs from its supplier quotation line.")
			base_type = quote_line.get("BaseType")
			source_line = quote_line

		if base_type != self.PURCHASE_REQUEST_TYPE:
			return None
		request_id = source_line.get("BaseEntry")
		request_lines = self.resolve(request_id, {
			"entity": "PurchaseRequests", "key": "DocEntry",
			"value_field": "DocumentLines", "key_type": "integer",
		})
		index, request_line = self._document_line(request_lines, source_line.get("BaseLine"))
		if request_line.get("ItemCode") != source_line.get("ItemCode"):
			raise frappe.ValidationError("SAP source line differs from its Purchase Request item.")
		return str(request_id), index + 1

	@staticmethod
	def _document_line(lines, line_number):
		if not isinstance(lines, list) or line_number is None:
			raise frappe.ValidationError("SAP source document lines are missing.")
		matches = [(index, line) for index, line in enumerate(lines) if line.get("LineNum") == line_number]
		if len(matches) != 1:
			raise frappe.ValidationError("SAP source document line did not resolve uniquely.")
		return matches[0]

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
		if cache_key not in self.cache and self._exists_from_preload(
			entity, key, value, config, extra_filter, cache_key
		):
			return self.cache[cache_key]
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

	def _exists_from_preload(self, entity, key, value, config, extra_filter, cache_key) -> bool:
		"""After many point lookups, read the matching keys once and answer from memory.

		Only integer keys qualify: string comparison in SAP may be case-insensitive.
		"""
		if not self.batching_enabled or config.get("key_type") != "integer":
			return False
		group = ("exists_group", entity, key, extra_filter)
		keys = self.cache.get(("exists_keys",) + group[1:])
		if keys is None:
			self._exists_misses[group] = self._exists_misses.get(group, 0) + 1
			if self._exists_misses[group] <= self.EXISTS_PRELOAD_AFTER:
				return False
			params = {"$select": key}
			if extra_filter:
				params["$filter"] = extra_filter.strip()
			try:
				rows = self.client.fetch_all(entity, params=params)
				keys = {str(int(row[key])) for row in rows if row.get(key) is not None}
			except Exception:
				self._exists_misses[group] = float("-inf")
				return False
			self.cache[("exists_keys",) + group[1:]] = keys
		self.cache[cache_key] = int(str(int(value)) in keys)
		return True

	def prime_documents(self, entity: str, key: str, ids, value_field: str) -> None:
		"""Fetch several source documents per request and cache what resolve() would return.

		A document that is missing or not unique is left uncached, so resolve() still runs
		its own query and raises its own error.
		"""
		if not self.batching_enabled:
			return
		literal = self._literal
		wanted = []
		for value in dict.fromkeys(ids):
			if value in (None, "") or isinstance(value, bool) or not str(value).lstrip("-").isdigit():
				continue
			if (entity, key, value_field, literal(value, "integer")) not in self.cache:
				wanted.append(int(value))
		for start in range(0, len(wanted), self.BATCH_ID_CHUNK):
			chunk = wanted[start:start + self.BATCH_ID_CHUNK]
			try:
				rows = self.client.fetch_all(entity, params={
					"$filter": " or ".join(f"{key} eq {value}" for value in chunk),
					"$select": f"{key},{value_field}",
				})
			except Exception:
				continue
			by_id: dict[str, list] = {}
			for row in rows:
				by_id.setdefault(str(row.get(key)), []).append(row)
			for value in chunk:
				matches = by_id.get(str(value), [])
				if len(matches) == 1 and value_field in matches[0]:
					self.cache[(entity, key, value_field, literal(value, "integer"))] = matches[0][value_field]

	def prime_purchase_order_sources(self, orders: list[dict]) -> None:
		lines = [line for order in orders for line in order.get("DocumentLines") or []]
		quotes = [l.get("BaseEntry") for l in lines if l.get("BaseType") == self.PURCHASE_QUOTATION_TYPE]
		requests = [l.get("BaseEntry") for l in lines if l.get("BaseType") == self.PURCHASE_REQUEST_TYPE]
		self.prime_documents("PurchaseQuotations", "DocEntry", quotes, "DocumentLines")
		for line in lines:
			if line.get("BaseType") != self.PURCHASE_QUOTATION_TYPE:
				continue
			quote_lines = self.cache.get((
				"PurchaseQuotations", "DocEntry", "DocumentLines", self._literal_or_none(line.get("BaseEntry"))
			))
			for quote_line in quote_lines or []:
				if quote_line.get("LineNum") == line.get("BaseLine") and quote_line.get("BaseType") == self.PURCHASE_REQUEST_TYPE:
					requests.append(quote_line.get("BaseEntry"))
		self.prime_documents("PurchaseRequests", "DocEntry", requests, "DocumentLines")

	def prime_purchase_invoice_receipts(self, invoices: list[dict]) -> None:
		receipts = [
			line.get("BaseEntry") for invoice in invoices for line in invoice.get("DocumentLines") or []
			if str(line.get("BaseType")) == "20"
		]
		self.prime_documents("PurchaseDeliveryNotes", "DocEntry", receipts, "DocumentLines")

	def _literal_or_none(self, value):
		try:
			return self._literal(value, "integer")
		except Exception:
			return None

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
