# Copyright (c) 2026, Mukesh Variyani and contributors
# One-time migration: rename synced Departments to site-suffixed names
# ("Human Resources" -> "Human Resources - CPC") so records from different
# source sites (CPC / SEC / IMC) stop colliding on the same local document.
import re

import frappe
from frappe.utils.nestedset import rebuild_tree


def execute():
    departments = frappe.get_all(
        "Department",
        filters={"source_site": ("is", "set")},
        fields=["name", "department_name", "source_site"],
    )

    renamed = 0
    for d in departments:
        base = (d.department_name or d.name).strip()
        site = (d.source_site or "").strip()
        if not site:
            continue

        target = base if re.search(rf"- {re.escape(site)}$", base) else f"{base} - {site}"
        if d.name == target:
            continue
        if frappe.db.exists("Department", target):
            # A department with the suffixed name already exists (unexpected);
            # skip rather than create a duplicate conflict.
            print(f"SKIP (target exists): {d.name} -> {target}")
            continue

        frappe.rename_doc("Department", d.name, target)
        renamed += 1

    # Keep department_name consistent with the (possibly renamed) doc name
    for d in frappe.get_all(
        "Department",
        filters={"source_site": ("is", "set")},
        fields=["name", "department_name"],
    ):
        if d.name != d.department_name:
            frappe.db.set_value("Department", d.name, "department_name", d.name)

    rebuild_tree("Department")
    print(f"Renamed {renamed} synced departments with site suffix")
