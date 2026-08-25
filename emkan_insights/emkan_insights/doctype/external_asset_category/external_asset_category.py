# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs


class ExternalAssetCategory(Document):
	pass


@frappe.whitelist()
@filter_kwargs
def sync_external_records(names, sync_now=False):
    if isinstance(names, str):
        names = json.loads(names)
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(
        source_doctype="External Asset Category",
        names=names,
        sync_now=sync_now,
    )
