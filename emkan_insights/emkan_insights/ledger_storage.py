"""Bulk persistence for high-volume external ledger rows."""

import json

import frappe
from pypika.terms import Values


LEDGER_UPSERT_CHUNK_SIZE = 200


def upsert_ledger_page(*, local_doctype, rows, company_abbr, configuration_name):
    if not rows:
        return

    fields = [
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "idx",
        "remote_id",
        "source_site",
        "last_synced_on",
        "json",
    ]
    conflict_fields = [
        "modified",
        "modified_by",
        "remote_id",
        "source_site",
        "last_synced_on",
        "json",
    ]
    now = frappe.utils.now()
    user = frappe.session.user or "Administrator"
    values = [
        _ledger_values(
            local_doctype,
            row,
            company_abbr,
            configuration_name,
            now,
            user,
        )
        for row in rows
    ]

    table = frappe.qb.DocType(local_doctype)
    for start in range(0, len(values), LEDGER_UPSERT_CHUNK_SIZE):
        query = (
            frappe.qb.into(table)
            .columns(fields)
            .insert(*values[start : start + LEDGER_UPSERT_CHUNK_SIZE])
        )
        query = _apply_upsert_clause(query, table, conflict_fields)
        query.run()


def _ledger_values(
    local_doctype,
    row,
    company_abbr,
    configuration_name,
    now,
    user,
):
    remote_id = str(row.get("name") or "").strip()
    if not remote_id:
        raise ValueError(f"{local_doctype} payload contains a row without a name")

    local_name = _prefixed_remote_id(company_abbr, remote_id)
    return (
        local_name,
        now,
        now,
        user,
        user,
        0,
        0,
        local_name,
        configuration_name,
        row.get("modified") or now,
        json.dumps(row, default=str, ensure_ascii=False),
    )


def _apply_upsert_clause(query, table, conflict_fields):
    if frappe.db.db_type == "mariadb":
        for fieldname in conflict_fields:
            query = query.on_duplicate_key_update(table[fieldname], Values(fieldname))
        return query

    if frappe.db.db_type == "postgres":
        query = query.on_conflict(table.name)
        for fieldname in conflict_fields:
            query = query.do_update(table[fieldname])
        return query

    raise NotImplementedError(f"Fast ledger fetch does not support {frappe.db.db_type}")


def _prefixed_remote_id(company_abbr, remote_id):
    if remote_id.startswith(f"{company_abbr}-"):
        return remote_id
    return f"{company_abbr}-{remote_id}"
