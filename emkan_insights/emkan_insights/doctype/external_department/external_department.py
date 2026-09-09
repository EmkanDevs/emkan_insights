# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import json
import re

import frappe
from frappe.model.document import Document
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs


class ExternalDepartment(Document):
    pass


GLOBAL_ROOT_DEPARTMENT = "All Departments"

SITE_SUFFIX_PATTERN_CACHE = {}


# ==========================================================
# HELPERS
# ==========================================================

def _target_department_name(department_name, site):
    """
    Local Department name for a source record.

    Records from different source sites (CPC / SEC / IMC) must NOT collide on
    the same local document, so the site is appended as a suffix:
      ("Human Resources", "CPC")  -> "Human Resources - CPC"
    Names already carrying their own site suffix are left untouched.
    """
    base = (department_name or "").strip()
    if not site:
        return base

    from emkan_insights.emkan_insights.sync_guard import get_canonical_site_code

    site = get_canonical_site_code(site) or site

    pattern = SITE_SUFFIX_PATTERN_CACHE.get(site)
    if pattern is None:
        pattern = re.compile(rf"- {re.escape(site)}$")
        SITE_SUFFIX_PATTERN_CACHE[site] = pattern

    if pattern.search(base):
        return base
    return f"{base} - {site}"


def _get_site_company(site):
    """Company configured on the site's External Site Configuration."""
    canonical = get_canonical_site_code_safe(site)
    if not canonical:
        return None
    return frappe.db.get_value("External Site Configuration", canonical, "company")


def _get_company_root_department(company):
    """
    Return the top-level Department ERPNext auto-creates when a Company is
    saved (department_name="All Departments", company=<company>, no parent).

    Scoping the lookup to the company means CPC's subtree never floats up
    into IMC's (or the global) root.
    """
    if not company:
        return None
    return frappe.db.get_value(
        "Department",
        {
            "company": company,
            "parent_department": ("in", ["", None, GLOBAL_ROOT_DEPARTMENT]),
            "is_group": 1,
        },
        "name",
    )


def _site_root_name(site):
    """Per-site tree root name: 'All Departments - <Site>'."""
    canonical = get_canonical_site_code_safe(site)
    return f"{GLOBAL_ROOT_DEPARTMENT} - {canonical}" if canonical else None


def _ensure_site_root_department(site):
    """
    Ensure the per-site tree root ('All Departments - <Site>') exists and
    return its name.

    The root is parented under the COMPANY'S own top-level Department
    (not the bare global root), so every site's subtree stays inside its
    company and ERPNext's company-scoped filters work correctly.

    Tree structure per site:
        All Departments                        <- global root (no company)
        └── All Departments  [company=CPC Co]  <- ERPNext company root
            └── All Departments - CPC          <- site root  (this node)
                ├── HR - CPC
                └── Finance - CPC

    Returns None when there is no canonical site/company.
    """
    if not site:
        return None
    root_name = _site_root_name(site)
    if not root_name:
        return None
    if frappe.db.exists("Department", root_name):
        return root_name

    company = _get_site_company(site)

    # Prefer the company-specific root; fall back to the global root only
    # when the company root hasn't been created yet (e.g. brand-new company).
    company_root = _get_company_root_department(company) if company else None
    parent = company_root or (
        GLOBAL_ROOT_DEPARTMENT
        if frappe.db.exists("Department", GLOBAL_ROOT_DEPARTMENT)
        else None
    )

    doc = frappe.new_doc("Department")
    doc.name = root_name
    doc.flags.name_set = True
    doc.department_name = root_name
    doc.is_group = 1
    if company:
        doc.company = company
    if parent:
        doc.parent_department = parent

    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.clear_document_cache("Department", root_name)
    return root_name


def _parent_ref_candidates(parent_ref, site):
    """
    Candidate values for a remote parent reference: the raw ref plus, when
    the doctype is company-prefixed on fetch, the '<ABBR>-<ref>' variant.
    """
    refs = [parent_ref]
    company = _get_site_company(site)
    if company:
        from emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration import (
            _get_company_abbr_for_sync,
            _build_company_prefixed_remote_id,
        )

        try:
            prefixed = _build_company_prefixed_remote_id(
                _get_company_abbr_for_sync(company), parent_ref
            )
        except Exception:
            prefixed = None
        if prefixed and prefixed != parent_ref:
            refs.append(prefixed)
    return refs


def _find_existing_department(remote_id, target_name, site=None):
    """
    Look up an existing Department:
      1. by remote_id stored on the Department doc (custom field)
      2. by exact suffixed name
    Lookups are scoped to the source site so records of one company never
    adopt another company's department document.
    Returns None when the department has never been synced.
    """
    if remote_id:
        for ref in _parent_ref_candidates(remote_id, site):
            filters = {"remote_id": ref}
            if site:
                filters["source_site"] = site
            existing = frappe.db.get_value("Department", filters, "name")
            if existing:
                return existing

    if target_name and frappe.db.exists("Department", target_name):
        return target_name

    return None


def _resolve_parent_department(src):
    """
    Resolve the LOCAL parent department for a source record.

    Parents are resolved STRICTLY within the same source site so e.g. CPC's
    "Executive" never points at IMC's (or the incharge company's) departments.
    Roots (no parent on the remote) attach under their OWN site root
    'All Departments - <Site>', keeping every company's subtree separate.

    Returns the local parent name, or None when the parent department has not
    been synced yet.
    """
    parent_ref = getattr(src, "parent_department", None)
    site = getattr(src, "source_site", None)

    # Root on the remote -> hang under this site's own tree root
    if not parent_ref or parent_ref == GLOBAL_ROOT_DEPARTMENT:
        return _ensure_site_root_department(site) or (
            GLOBAL_ROOT_DEPARTMENT if frappe.db.exists("Department", GLOBAL_ROOT_DEPARTMENT) else None
        )

    # Remote rows are fetched with a company-abbr prefix on name/id, so the
    # parent ref may appear in raw or '<ABBR>-<ref>' form.
    refs = _parent_ref_candidates(parent_ref, site)

    # 1. Find the External Department row this parent ref points at.
    #    Same site only — never fall back to another company's rows.
    parent_ext_name = None
    if site:
        parent_ext_name = (
            frappe.db.get_value(
                "External Department",
                {"source_site": site, "remote_id": ("in", refs)},
                "name",
            )
            or frappe.db.get_value(
                "External Department",
                {"source_site": site, "name": ("in", refs)},
                "name",
            )
        )
    else:
        parent_ext_name = frappe.db.get_value(
            "External Department", {"remote_id": ("in", refs)}, "name"
        ) or (parent_ref if frappe.db.exists("External Department", parent_ref) else None)

    # 2. Map that row to its local suffixed Department document.
    if parent_ext_name:
        p = frappe.db.get_value(
            "External Department",
            parent_ext_name,
            ["department_name", "source_site"],
            as_dict=True,
        )
        if p:
            target = _target_department_name(p.department_name or parent_ext_name, p.source_site)
            if target and frappe.db.exists("Department", target):
                return target

        # Same-site Department already carrying the ref as remote_id
        dept_filters = {"remote_id": ("in", refs)}
        if site:
            dept_filters["source_site"] = site
        found_by_remote_id = frappe.db.get_value("Department", dept_filters, "name")
        if found_by_remote_id:
            return found_by_remote_id

    # Last resort: a local department literally named like the remote parent —
    # accepted only when it belongs to the SAME source site.
    for ref in refs:
        if frappe.db.exists("Department", ref):
            if not site:
                return ref
            parent_site = frappe.db.get_value("Department", ref, "source_site")
            if not parent_site:
                canonical = get_canonical_site_code_safe(site)
                if ref.endswith(f"- {canonical}"):
                    return ref
            elif get_canonical_site_code_safe(parent_site) == get_canonical_site_code_safe(site):
                return ref

    return None


def get_canonical_site_code_safe(site):
    from emkan_insights.emkan_insights.sync_guard import get_canonical_site_code

    return get_canonical_site_code(site) or site


def _has_children(department):
    """
    True when the local Department already has child nodes in the tree.
    Nested-set validation forbids such nodes from being leaves (is_group=0).
    """
    bounds = frappe.db.get_value("Department", department, ["lft", "rgt"], as_dict=True)
    if not bounds:
        return False
    return bool(
        frappe.db.count(
            "Department", {"lft": (">", bounds.lft), "rgt": ("<", bounds.rgt)}
        )
    )


def _collect_force_group_names(names):
    """
    Local Department names that act as PARENTS for this batch:
      1. names referenced as parent_department by another row
      2. local departments that already have children in the tree
    Such departments must be synced with is_group=1 or nested-set validation
    rejects them ("cannot be a leaf node as it has children").
    """
    rows = frappe.get_all(
        "External Department",
        filters={"name": ("in", names)},
        fields=[
            "name",
            "department_name",
            "source_site",
            "remote_id",
            "parent_department",
        ],
    )

    target_by_row = {}
    lookup = {}
    for r in rows:
        target_by_row[r.name] = _target_department_name(
            r.department_name or r.name, r.source_site
        )
        if getattr(r, "remote_id", None):
            lookup[(r.source_site, "rid", r.remote_id)] = r.name
        lookup[(r.source_site, "name", r.name)] = r.name

    force_groups = set()

    for r in rows:
        ref = r.parent_department
        if not ref or ref == GLOBAL_ROOT_DEPARTMENT:
            continue
        hit_name = (
            lookup.get((r.source_site, "rid", ref))
            or lookup.get((r.source_site, "name", ref))
            or lookup.get((None, "rid", ref))
            or lookup.get((None, "name", ref))
        )
        if hit_name:
            force_groups.add(target_by_row[hit_name])

    # Local departments that already have children must stay groups
    for target in set(target_by_row.values()):
        if frappe.db.exists("Department", target) and _has_children(target):
            force_groups.add(target)

    return force_groups


def _is_descendant(child_name, parent_name):
    """
    True when `child_name` sits inside the subtree of `parent_name`.
    Frappe's nested-set validation rejects moving a node into its own subtree.
    """
    child_bounds = frappe.db.get_value("Department", child_name, ["lft", "rgt"], as_dict=True)
    parent_bounds = frappe.db.get_value(
        "Department", parent_name, ["lft", "rgt"], as_dict=True
    ) if child_bounds else None
    if not child_bounds or not parent_bounds:
        return False
    return child_bounds.lft < parent_bounds.lft and parent_bounds.rgt < child_bounds.rgt


# ==========================================================
# TREE SYNC (two passes: create nodes, then link the tree)
# ==========================================================

def upsert_department(src, existing_name, force_group=False):
    """
    Pass 1: upsert a Department's fields WITHOUT its tree position.

    Every department is created first (flat) so that any parent referenced
    in pass 2 already exists, no matter what order the source rows arrive in.

    Departments that act as parents (referenced by another row, or already
    having children locally — e.g. pre-existing CPC departments) are forced
    to is_group=1; nested-set validation rejects leaf nodes with children.

    Naming convention: local name = "<department_name> - <source_site>"
    so records from different sites never overwrite each other.
    The Department's remote_id stores the final local name for lookups.
    """
    site = getattr(src, "source_site", None)
    target_name = _target_department_name(
        getattr(src, "department_name", None) or src.name,
        site,
    )

    company = getattr(src, "company", None) or _get_site_company(site)
    disabled = int(getattr(src, "disabled", 0) or 0)

    # A department with children can never be a leaf node
    if existing_name and _has_children(existing_name):
        force_group = True
    is_group = 1 if (force_group or int(getattr(src, "is_group", 0) or 0)) else 0

    dept_meta = frappe.get_meta("Department")
    has_remote_id = dept_meta.has_field("remote_id")
    has_source_site = dept_meta.has_field("source_site")

    if existing_name:
        doc = frappe.get_doc("Department", existing_name)
        doc.flags.ignore_permissions = True

        doc.department_name = target_name
        doc.company = company
        doc.is_group = is_group
        doc.disabled = disabled

        if has_remote_id:
            doc.remote_id = target_name
        if has_source_site:
            doc.source_site = site

        doc.save()
        final_name = doc.name

        # Legacy unsuffixed document: bring it to the new naming scheme.
        if final_name != target_name and not frappe.db.exists("Department", target_name):
            # validate=False bypasses ERPNext before_rename which re-appends
            # the company abbreviation (e.g. "- IMC") and breaks the rename.
            from frappe.model.rename_doc import rename_doc as _rename_doc

            _rename_doc("Department", final_name, target_name, validate=False)
            final_name = target_name

    else:
        doc = frappe.new_doc("Department")
        doc.name = target_name
        doc.flags.name_set = True

        update_dict = {
            "department_name": target_name,
            "company": company,
            "is_group": is_group,
            "disabled": disabled,
        }
        # Park new nodes under their site root (or the company/global root) so
        # the tree stays valid until pass 2 positions them correctly.
        park_root = _site_root_name(site)
        if not (park_root and frappe.db.exists("Department", park_root)):
            # Fall back: company root → global root
            company_root = _get_company_root_department(company) if company else None
            park_root = company_root or (
                GLOBAL_ROOT_DEPARTMENT
                if frappe.db.exists("Department", GLOBAL_ROOT_DEPARTMENT)
                else None
            )
        if park_root:
            update_dict["parent_department"] = park_root
        if has_remote_id:
            update_dict["remote_id"] = target_name
        if has_source_site:
            update_dict["source_site"] = site
        doc.update(update_dict)

        doc.flags.ignore_permissions = True
        doc.save()

        final_name = doc.name

    ext_meta = frappe.get_meta("External Department")
    if ext_meta.has_field("remote_id"):
        src.db_set("remote_id", final_name, update_modified=False)

    frappe.clear_document_cache("Department", final_name)
    return final_name


def link_department_parent(src, final_name):
    """
    Pass 2: place the department in the tree by setting parent_department.

    Runs after ALL departments exist, so parents always resolve. Saving the
    doc lets Frappe's nested-set handling move the node safely.
    """
    parent_department = _resolve_parent_department(src)

    # Cycle guards — never move a node into its own subtree
    if parent_department and parent_department in {final_name, src.name}:
        return
    if parent_department and _is_descendant(final_name, parent_department):
        return

    # Parent not synced (partial selection)? Keep the node inside its
    # site's subtree rather than ripping it out of the tree.
    if not parent_department:
        parent_department = _ensure_site_root_department(getattr(src, "source_site", None))

    current_parent = frappe.db.get_value("Department", final_name, "parent_department")
    if current_parent == parent_department:
        return

    # A node receiving children must be a group, or nested-set validation
    # rejects the move ("cannot be a leaf node as it has children").
    if parent_department and parent_department != GLOBAL_ROOT_DEPARTMENT:
        if not int(frappe.db.get_value("Department", parent_department, "is_group") or 0):
            frappe.db.set_value(
                "Department", parent_department, "is_group", 1, update_modified=False
            )
            frappe.clear_document_cache("Department", parent_department)

    doc = frappe.get_doc("Department", final_name)
    doc.flags.ignore_permissions = True
    doc.parent_department = parent_department
    doc.save()

    frappe.clear_document_cache("Department", final_name)


# ==========================================================
# ENTRY POINTS
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_department_docs(source_doctype: str, names, sync_now=False):
    """
    Sync External Department records to the Department doctype.

    When called from the sequential pipeline (sync_now=True), runs synchronously.
    When called from the client, enqueues a background job.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if sync_now:
        return sync_bulk_departments(names)

    frappe.enqueue(
        "emkan_insights.emkan_insights.doctype.external_department.external_department.sync_bulk_departments",
        queue="long",
        names=names,
        timeout=2000,
    )
    return "Sync Queued"


def sync_bulk_departments(names):
    """
    Bulk sync departments from External Department to Department as a TREE.

    Two passes:
      1. Create/update every department flat (no parent links), guaranteeing
         all nodes exist.
      2. Link parent_department for each node, building the full tree.

    Errors are NOT swallowed — the first hard failure raises with the exact
    original exception.
    """
    created = []
    force_groups = _collect_force_group_names(names)
    sites = set()

    # Pass 1: ensure every node exists (order-independent)
    for name in names:
        src = frappe.get_doc("External Department", name)
        site = getattr(src, "source_site", None)
        sites.add(site)
        target_name = _target_department_name(
            getattr(src, "department_name", None) or src.name,
            site,
        )
        existing = _find_existing_department(
            getattr(src, "remote_id", None),
            target_name,
            site=site,
        )
        final_name = upsert_department(src, existing, force_group=target_name in force_groups)
        created.append((src, name, final_name))

    # Per-company subtrees: make sure each site root exists before linking
    for site in sites:
        if site:
            _ensure_site_root_department(site)

    # Pass 2: build the tree once all nodes exist
    for src, name, final_name in created:
        link_department_parent(src, final_name)

    return [
        {"name": name, "status": "success", "department_name": final_name}
        for _, name, final_name in created
    ]
