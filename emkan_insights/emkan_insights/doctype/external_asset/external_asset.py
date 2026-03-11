# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ExternalAsset(Document):
	pass


@frappe.whitelist()
def sync_external_records(names):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(source_doctype="External Asset", names=names)
