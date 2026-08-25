# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs


class ExternalCustomerGroup(Document):
	pass


@frappe.whitelist()
@filter_kwargs
def sync_external_records(names):
	import json
	from emkan_insights.emkan_insights.external_sync import sync_external_docs

	if isinstance(names, str):
		names = json.loads(names)

	return sync_external_docs(source_doctype="External Customer Group", names=names)
