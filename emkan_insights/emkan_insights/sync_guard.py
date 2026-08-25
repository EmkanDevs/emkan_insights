from contextlib import contextmanager

import frappe


def _lock_name(doctype, name):
    return f"emkan_insights:{doctype}:{name}"[:64]


def acquire_sync_lock(doctype, name, lock_timeout=30):
    lock_name = _lock_name(doctype, name)
    result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, lock_timeout))
    acquired = result and result[0] and int(result[0][0] or 0) == 1

    if not acquired:
        frappe.throw(f"Could not acquire sync lock for {doctype} {name}")

    return lock_name


def release_sync_lock(lock_name):
    if lock_name:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)


@contextmanager
def sync_record_guard(doctype, name, lock_timeout=30):
    lock_name = acquire_sync_lock(doctype, name, lock_timeout=lock_timeout)
    savepoint = f"sync_record_{frappe.generate_hash(length=10)}"
    frappe.db.savepoint(savepoint)

    try:
        yield
    except Exception:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            frappe.db.rollback()
        raise
    finally:
        release_sync_lock(lock_name)


def _collect_item_codes(value, item_codes):
    if not value:
        return

    if isinstance(value, str):
        item_codes.add(value)
        return

    if isinstance(value, dict):
        for fieldname in ("item_code", "item", "production_item"):
            if value.get(fieldname):
                item_codes.add(value.get(fieldname))
        for child_value in value.values():
            if isinstance(child_value, list):
                _collect_item_codes(child_value, item_codes)
        return

    if isinstance(value, list):
        for row in value:
            _collect_item_codes(row, item_codes)


def get_document_item_codes(doc):
    item_codes = set()

    for fieldname in ("item_code", "item", "production_item"):
        value = doc.get(fieldname)
        if value:
            item_codes.add(value)

    for row in doc.get_all_children():
        for fieldname in ("item_code", "item", "production_item"):
            value = row.get(fieldname)
            if value:
                item_codes.add(value)

    return {item_code for item_code in item_codes if frappe.db.exists("Item", item_code)}


def _source_site_value_for_field(df, configuration_name=None, site_url=None):
    if df.fieldtype == "Link" and df.options == "External Site Configuration":
        return configuration_name or get_external_site_configuration_name(site_url)

    return site_url or configuration_name


def get_external_site_configuration_name(site_url):
    if not site_url:
        return None

    clean_url = str(site_url).rstrip("/")
    return (
        frappe.db.get_value("External Site Configuration", {"site_url": clean_url}, "name")
        or frappe.db.get_value("External Site Configuration", {"site_url": ["like", f"{clean_url}/%"]}, "name")
        or frappe.db.get_value("External Site Configuration", {"site_url": ["like", f"{clean_url}%"]}, "name")
    )


def set_source_site_fields(doc, configuration_name=None, site_url=None):
    meta = frappe.get_meta(doc.doctype)

    for fieldname in ("source_site", "custom_source_site"):
        df = meta.get_field(fieldname)
        if not df:
            continue

        value = _source_site_value_for_field(df, configuration_name, site_url)
        if value:
            doc.set(fieldname, value)

    if meta.get_field("site_url") and site_url:
        doc.set("site_url", site_url)


@contextmanager
def temporarily_enable_transaction_items(doc):
    disabled_items = [
        item_code
        for item_code in get_document_item_codes(doc)
        if frappe.db.get_value("Item", item_code, "disabled")
    ]

    if not disabled_items:
        yield
        return

    try:
        for item_code in disabled_items:
            frappe.db.set_value("Item", item_code, "disabled", 0, update_modified=False)
        yield
    finally:
        for item_code in disabled_items:
            frappe.db.set_value("Item", item_code, "disabled", 1, update_modified=False)
