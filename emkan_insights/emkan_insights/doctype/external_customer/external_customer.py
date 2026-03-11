# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import json
from typing import List, Union

class ExternalCustomer(Document):
	pass

@frappe.whitelist()
def sync_external_customers(external_customers: Union[str, List[str]]):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(source_doctype="External Customer", names=external_customers)

@frappe.whitelist()
def sync_external_records(names):
    from emkan_insights.emkan_insights.external_sync import sync_external_docs
    return sync_external_docs(source_doctype="External Customer", names=names)
