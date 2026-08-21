# Copyright (c) 2026, Mukesh Variyani and contributors
import frappe
import json
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs

class ExternalBankAccount(Document):
	pass

@frappe.whitelist()
@filter_kwargs
def sync_external_records(names):
    if isinstance(names, str):
        names = json.loads(names)
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(source_doctype="External Bank Account", names=names)
