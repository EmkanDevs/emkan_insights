import frappe


def order_tree_sync_names(
    source_doctype: str,
    names,
    parent_field: str,
    key_field: str = "remote_id",
) -> list[str]:
    """Return requested tree records with all available ancestors first."""
    ordered = []
    visited = set()
    visiting = []

    def visit(source_name):
        if source_name in visited:
            return
        if source_name in visiting:
            cycle_start = visiting.index(source_name)
            cycle = visiting[cycle_start:] + [source_name]
            frappe.throw(
                f"Circular {source_doctype} hierarchy: {' -> '.join(cycle)}"
            )

        visiting.append(source_name)
        parent_reference = frappe.db.get_value(
            source_doctype,
            source_name,
            parent_field,
        )
        parent_name = resolve_tree_source_name(
            source_doctype,
            parent_reference,
            key_field,
        )
        if parent_name:
            visit(parent_name)

        visiting.pop()
        visited.add(source_name)
        ordered.append(source_name)

    for name in dict.fromkeys(names or []):
        visit(name)

    return ordered


def resolve_tree_source_name(
    source_doctype: str,
    reference: str | None,
    key_field: str,
) -> str | None:
    if not reference:
        return None
    if frappe.db.exists(source_doctype, reference):
        return reference
    if not frappe.get_meta(source_doctype).has_field(key_field):
        return None

    return frappe.db.get_value(source_doctype, {key_field: reference}, "name")
