# Copyright (c) 2026, Mukesh Variyani and contributors
import frappe
# For license information, please see license.txt
import json

# import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs

class ExternalUom(Document):
	pass

@frappe.whitelist()
@filter_kwargs
def sync_external_records(names):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    if isinstance(names, str):
        names = json.loads(names)
    return sync_external_docs(source_doctype="External Uom", names=names)
