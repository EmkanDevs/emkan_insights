"""
Disables Frappe's Data-field validations (Phone, Email, Name, URL, IBAN)
while external sync/fetch jobs are running.

Remote data with values like "'+966 50 330 0419" can abort the whole sync.
This module
monkeypatches that method so it becomes a no-op whenever
``frappe.flags.in_external_sync`` is set. Normal desk/UI validation is
unaffected.
"""

from contextlib import contextmanager

import frappe
from frappe.model.base_document import BaseDocument

_original_validate_data_fields = BaseDocument._validate_data_fields


def _patched_validate_data_fields(self):
    if getattr(frappe.flags, "in_external_sync", False):
        return
    return _original_validate_data_fields(self)


def install_sync_validation_bypass():
    BaseDocument._validate_data_fields = _patched_validate_data_fields


@contextmanager
def data_field_validation_disabled():
    previous = getattr(frappe.flags, "in_external_sync", False)
    frappe.flags.in_external_sync = True
    try:
        yield
    finally:
        frappe.flags.in_external_sync = previous


install_sync_validation_bypass()
