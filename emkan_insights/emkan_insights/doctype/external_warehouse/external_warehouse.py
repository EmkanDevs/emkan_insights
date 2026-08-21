# Copyright (c) 2026, Mukesh Variyani and contributors
import frappe
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs

class ExternalWarehouse(Document):
	pass

@frappe.whitelist()
@filter_kwargs
def sync_external_records(names):
    import json
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    # When called from scheduler, names is already a Python list.
    # When called from the UI via whitelist, names arrives as a JSON string.
    # Normalise to a list here so sync_external_docs always receives a list.
    if isinstance(names, str):
        names = json.loads(names)
    return sync_external_docs(source_doctype="External Warehouse", names=names)
