"""Shared file-log helpers for external fetch and sync observability."""

import frappe


def log_fetch_plan(ref_doctype, configuration_name, filters, total_records):
    frappe.logger("external_sync").info(
        "Fetch Plan: %s | Site: %s | Records: %s | Remote filters: %s",
        ref_doctype,
        configuration_name,
        total_records,
        filters,
    )


def log_sync_plan(ref_doctype, configuration_name, filter_description, total_records):
    frappe.logger("external_sync").info(
        "Sync Plan: %s | Site: %s | Records: %s | Filters: %s",
        ref_doctype,
        configuration_name,
        total_records,
        filter_description,
    )
