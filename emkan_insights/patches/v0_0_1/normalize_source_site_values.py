# Copyright (c) 2026, Mukesh Variyani and contributors
# One-time repair: source_site values were stored inconsistently (config names,
# raw URLs, trailing slashes), producing Department names like
#   "Facility - https://cpcmg.emkan-dev.com/ - CPC - https://cpcmg.emkan-dev.com/"
# This patch strips every known site-variant suffix, applies ONE clean suffix
# based on the canonical External Site Configuration name, and normalizes the
# source_site values on both Department and External Department records.
import re

import frappe
from frappe.utils.nestedset import rebuild_tree

from frappe.model.rename_doc import rename_doc as _rename_doc

from emkan_insights.emkan_insights.sync_guard import get_canonical_site_code


# Informal site codes that appear inside synced names but are NOT
# External Site Configuration names ("All Departments - IMC - Incharge",
# "Legal - https://esacon.frappe.cloud/ - SEC - Saad Ali ...").
# They are stripped from names but never used as suffixes.
INFORMAL_SITE_CODES = {"IMC", "SEC"}


def _site_variants():
    """Map every known site identifier variant -> canonical config name."""
    variants = {}
    for r in frappe.get_all("External Site Configuration", fields=["name", "site_url"]):
        for v in {r.name, (r.site_url or "").strip(), (r.site_url or "").rstrip("/")}:
            if v:
                variants[v] = r.name
    return variants


def _strip_variants():
    return set(_site_variants()) | INFORMAL_SITE_CODES


def _strip_site_suffixes(name, variants):
    """Repeatedly remove any trailing ' - <variant>' segment."""
    changed = True
    while changed:
        changed = False
        for v in variants:
            suffix = f" - {v}"
            if name.endswith(suffix):
                name = name[: -len(suffix)].rstrip()
                changed = True
    return name


def _apply_suffix(base, canon):
    if not canon:
        return base
    if re.search(rf"- {re.escape(canon)}$", base):
        return base
    return f"{base} - {canon}"


def _safe_rename(doctype, old, new, merge=False):
    if old == new:
        return False
    try:
        # validate=False bypasses ERPNext Department.before_rename, which
        # re-appends the company abbreviation (e.g. "- IMC", "- SEC") and
        # makes the rename a no-op / ValidationError.
        _rename_doc("Department", old, new, merge=merge, validate=False)
        return True
    except frappe.ValidationError as e:
        # Tolerate races/no-ops ("old and new name are the same")
        if "same" in str(e):
            return False
        raise


def _canonicalize_department_source_sites(variants):
    fixed = 0
    names = frappe.get_all(
        "Department",
        filters={"source_site": ("is", "set")},
        pluck="name",
        order_by="lft",
    )
    for name in names:
        d = frappe.db.get_value(
            "Department", name,
            ["name", "department_name", "source_site"],
            as_dict=True,
        )
        if not d or not d.source_site:
            continue

        canon = get_canonical_site_code(d.source_site)
        if not canon:
            print(f"SKIP (unknown site): {d.name} [{d.source_site}]")
            continue

        base = _strip_site_suffixes((d.department_name or d.name).strip(), variants)
        target = _apply_suffix(base, canon)

        if d.name != target or d.department_name != target:
            print(f"PLAN: {d.name!r} -> {repr(target)} (base={base!r}, site={canon!r})")

        if d.source_site != canon:
            frappe.db.set_value("Department", d.name, "source_site", canon)

        if d.department_name != target:
            frappe.db.set_value("Department", d.name, "department_name", target)

        if d.name != target:
            existing_target = frappe.db.get_value(
                "Department", target, ["name", "source_site"], as_dict=True
            )
            if existing_target and existing_target.source_site == canon:
                # Duplicate from an earlier partial run: merge into the
                # canonical document (moves child/parent links over).
                print(f"MERGE: {d.name} -> {target}")
                fixed += int(_safe_rename("Department", d.name, target, merge=True))
            elif not existing_target:
                fixed += int(_safe_rename("Department", d.name, target))
            else:
                print(f"SKIP (target exists on another site): {d.name} -> {target}")
    return fixed


def _canonicalize_external_department_source_sites():
    rows = frappe.get_all(
        "External Department",
        fields=["name", "source_site"],
    )
    fixed = 0
    for r in rows:
        if not r.source_site:
            continue
        canon = get_canonical_site_code(r.source_site)
        if canon and canon != r.source_site:
            frappe.db.set_value("External Department", r.name, "source_site", canon)
            fixed += 1
    return fixed


def execute():
    variants = _strip_variants()

    renamed = _canonicalize_department_source_sites(variants)
    ext_fixed = _canonicalize_external_department_source_sites()

    # Re-sync department_name of renamed docs is handled above; make sure the
    # nested-set tree is still consistent after all the renames.
    rebuild_tree("Department")

    print(f"Renamed {renamed} departments; normalized {ext_fixed} External Department source sites")


# Allow running directly: bench --site <site> console >> execute()
if __name__ == "__main__":
    execute()
