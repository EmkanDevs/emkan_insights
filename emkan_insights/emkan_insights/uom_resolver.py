import frappe


def resolve_uom(uom_name):
    """
    Resolve a UOM name to the exact name stored in the local UOM master,
    using a case-insensitive match.

    Examples:
        ROLLS -> Rolls
        roll  -> Roll
        TRIP  -> TRIP   (when local master stores "TRIP")

    Roll and Rolls remain distinct; only casing is normalised.

    Raises frappe.ValidationError when no local UOM matches, so the caller's
    sync record fails with a clear message rather than silently using wrong data.

    Results are cached on frappe.local (scoped to the current request/job).
    """
    if not uom_name:
        frappe.throw(
            f"UOM value is empty; cannot resolve against the local UOM master.",
            exc=frappe.ValidationError,
        )

    # Per-request cache — avoids repeated DB hits without leaking across jobs.
    if not hasattr(frappe.local, "_uom_resolve_cache"):
        frappe.local._uom_resolve_cache = {}

    cache = frappe.local._uom_resolve_cache
    if uom_name in cache:
        return cache[uom_name]

    result = frappe.db.sql(
        """
        SELECT name
        FROM `tabUOM`
        WHERE LOWER(name) = LOWER(%s)
        LIMIT 1
        """,
        (uom_name,),
        as_list=True,
    )

    if not result:
        frappe.throw(
            f"UOM '{uom_name}' not found in the local UOM master. "
            f"Create the UOM or correct the value on the External document before syncing.",
            exc=frappe.ValidationError,
        )

    resolved = result[0][0]
    cache[uom_name] = resolved
    return resolved


def resolve_uom_or_none(uom_name):
    """
    Same as resolve_uom() but returns None when uom_name is empty.
    Use for optional UOM fields such as weight_uom or Item Price uom.
    """
    if not uom_name:
        return None
    return resolve_uom(uom_name)
