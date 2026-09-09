import frappe


SALES_COMPANY_ADDRESS_FIELDS = ("dispatch_address_name", "company_address")
PURCHASE_COMPANY_ADDRESS_FIELDS = ("billing_address", "shipping_address")
DROP_SHIP_ADDRESS_FIELDS = {"dispatch_address_name", "shipping_address"}


def custom_validate_company_linked_addresses(self):
	address_fields = _get_company_address_fields(self.doctype)
	if not address_fields:
		return

	is_drop_ship = self.doctype in {"Purchase Order", "Sales Order", "Sales Invoice"} and self.is_drop_ship(
		self.items
	)

	for fieldname in address_fields:
		address = self.get(fieldname)
		if not address or (is_drop_ship and fieldname in DROP_SHIP_ADDRESS_FIELDS):
			continue

		if not frappe.db.exists(
			"Dynamic Link",
			{
				"parent": address,
				"parenttype": "Address",
				"link_doctype": "Company",
				"link_name": self.company,
			},
		):
			self.set(fieldname, None)


def custom_validate_party_address_and_contact(self):
	party_type, party = self.get_party()
	if not (party_type and party):
		return

	address_fields = _get_party_address_fields(party_type)
	if any(self.get(fieldname) for fieldname in address_fields):
		_clear_unlinked_fields(self, address_fields, _get_party_links(party_type, party, "Address"))

	if self.get("contact_person"):
		_clear_unlinked_fields(
			self,
			("contact_person",),
			_get_party_links(party_type, party, "Contact"),
		)


def _get_company_address_fields(doctype):
	if doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice"):
		return SALES_COMPANY_ADDRESS_FIELDS
	if doctype in ("Purchase Order", "Purchase Receipt", "Purchase Invoice", "Supplier Quotation"):
		return PURCHASE_COMPANY_ADDRESS_FIELDS
	return ()


def _get_party_address_fields(party_type):
	if party_type == "Customer":
		return ("customer_address", "shipping_address_name")
	if party_type == "Supplier":
		return ("supplier_address",)
	return ()


def _get_party_links(party_type, party, parenttype):
	return set(
		frappe.get_all(
			"Dynamic Link",
			{
				"link_doctype": party_type,
				"link_name": party,
				"parenttype": parenttype,
			},
			pluck="parent",
		)
	)


def _clear_unlinked_fields(doc, fieldnames, valid_links):
	for fieldname in fieldnames:
		value = doc.get(fieldname)
		if value and value not in valid_links:
			doc.set(fieldname, None)
