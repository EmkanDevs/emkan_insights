from contextlib import contextmanager

import frappe
from frappe.utils import flt


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


def get_stock_entry_warehouses(doc):
    warehouses = {
        doc.get("from_warehouse"),
        doc.get("to_warehouse"),
    }

    for row in doc.get("items", []):
        warehouses.update((row.get("s_warehouse"), row.get("t_warehouse")))

    return {warehouse for warehouse in warehouses if warehouse}


def _source_site_value_for_field(df, configuration_name=None, site_url=None):
    if df.fieldtype == "Link" and df.options == "External Site Configuration":
        return configuration_name or get_external_site_configuration_name(site_url)

    # Always store the canonical External Site Configuration name so
    # source-site values stay consistent across records (never raw URLs).
    if configuration_name:
        return configuration_name
    return get_external_site_configuration_name(site_url) or site_url


def get_canonical_site_code(site):
    """
    Map any source-site variant (configuration name, bare URL, URL with or
    without trailing slash) to the canonical External Site Configuration name.
    Returns None when no configuration matches.
    """
    if not site:
        return None

    s = str(site).strip()
    if not s:
        return None

    if frappe.db.exists("External Site Configuration", s):
        return s

    clean = s.rstrip("/")
    return frappe.db.get_value(
        "External Site Configuration",
        {"site_url": ("in", (s, clean, f"{clean}/"))},
        "name",
    )


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
def party_validation_disabled():
    """Bypass disabled and frozen party checks during a sync operation."""
    previous_value = frappe.flags.ignore_party_validation
    frappe.flags.ignore_party_validation = True

    try:
        yield
    finally:
        frappe.flags.ignore_party_validation = previous_value


def enable_supplier_for_sync(supplier):
    """Enable and unfreeze a supplier before transaction validation."""
    state = frappe.db.get_value(
        "Supplier",
        supplier,
        ["disabled", "is_frozen"],
        as_dict=True,
    )
    if not state:
        return False

    updates = {}
    if frappe.utils.cint(state.get("disabled")):
        updates["disabled"] = 0
    if frappe.utils.cint(state.get("is_frozen")):
        updates["is_frozen"] = 0
    if not updates:
        return False

    frappe.db.set_value("Supplier", supplier, updates)
    frappe.clear_document_cache("Supplier", supplier)
    return True


@contextmanager
def active_item_during_opening_stock(doc):
    """Keep Item masters active while ERPNext creates opening stock."""
    if (
        getattr(doc, "doctype", None) == "Stock Reconciliation"
        and doc.get("purpose") == "Opening Stock"
    ):
        with temporarily_enable_transaction_items(doc):
            yield
        return

    if getattr(doc, "doctype", None) != "Item" or not doc.is_new() or not doc.opening_stock:
        yield
        return

    disabled = doc.disabled
    end_of_life = doc.end_of_life
    doc.disabled = 0
    doc.end_of_life = None

    try:
        yield
    finally:
        doc.disabled = disabled
        doc.end_of_life = end_of_life

        if not doc.is_new():
            frappe.db.set_value(
                "Item",
                doc.name,
                {"disabled": disabled, "end_of_life": end_of_life},
                update_modified=False,
            )


@contextmanager
def copied_link_validation_disabled():
    """
    Disable Frappe/ERPNext checks on header link fields copied from the
    remote site. Two validators run inside save/submit:

    - Document._validate_links throws "Could not find ..." when a copied
      value points to a record that does not exist locally.
    - AccountsController.validate_party_address_and_contact throws
      "... does not belong to <party>" when a copied Address/Contact is
      not Dynamic-Linked to this doc's party — common because contact
      links resolve against remote party names, which can differ from
      local ones (e.g. SUP-0197 vs the remote supplier code).

    Both are skipped so a stale remote reference survives the copy
    instead of aborting the whole record.
    """
    from frappe.model.document import Document

    try:
        from erpnext.controllers.accounts_controller import AccountsController
    except ImportError:
        AccountsController = None

    original_validate_links = Document._validate_links
    Document._validate_links = lambda self: None

    original_party_validation = None
    if AccountsController is not None:
        original_party_validation = (
            AccountsController.validate_party_address_and_contact
        )
        AccountsController.validate_party_address_and_contact = lambda self: None

    try:
        yield
    finally:
        Document._validate_links = original_validate_links
        if AccountsController is not None:
            AccountsController.validate_party_address_and_contact = (
                original_party_validation
            )


@contextmanager
def preserve_copied_outgoing_rates():
    """
    Stock Entry sync copies authoritative rates from the remote site, but
    ERPNext's validate() calls calculate_rate_and_amount() with
    reset_outgoing_rate=True, which recomputes every outgoing row's
    basic_rate from the LOCAL stock ledger (set_basic_rate_manually only
    protects incoming/finished rows). When inbound documents were synced
    without valuation that local average is 0 and silently overwrites the
    copied rate. Force reset_outgoing_rate=False so copied rates survive,
    and use the copied child-row valuation when the local stock ledger needs
    a fallback rate for an outgoing entry.
    """
    try:
        from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry
        from erpnext.stock.stock_ledger import update_entries_after
    except ImportError:
        yield
        return

    original_calculate_rate_and_amount = StockEntry.calculate_rate_and_amount
    original_get_fallback_rate = update_entries_after.get_fallback_rate

    def calculate_rate_and_amount(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
        return original_calculate_rate_and_amount(self, False, raise_error_if_no_rate)

    def get_fallback_rate(self, sle):
        if sle.voucher_type == "Stock Entry" and sle.voucher_detail_no:
            copied_rates = frappe.db.get_value(
                "Stock Entry Detail",
                sle.voucher_detail_no,
                ["valuation_rate", "basic_rate"],
                as_dict=True,
            ) or {}
            copied_rate = flt(copied_rates.get("valuation_rate")) or flt(
                copied_rates.get("basic_rate")
            )
            if copied_rate:
                return copied_rate

        return original_get_fallback_rate(self, sle)

    StockEntry.calculate_rate_and_amount = calculate_rate_and_amount
    update_entries_after.get_fallback_rate = get_fallback_rate
    try:
        yield
    finally:
        StockEntry.calculate_rate_and_amount = original_calculate_rate_and_amount
        update_entries_after.get_fallback_rate = original_get_fallback_rate


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


@contextmanager
def temporarily_enable_stock_entry_warehouses(doc):
    disabled_warehouses = [
        warehouse
        for warehouse in get_stock_entry_warehouses(doc)
        if frappe.db.get_value("Warehouse", warehouse, "disabled")
    ]

    if not disabled_warehouses:
        yield
        return

    try:
        for warehouse in disabled_warehouses:
            frappe.db.set_value(
                "Warehouse",
                warehouse,
                "disabled",
                0,
                update_modified=False,
            )
        yield
    finally:
        for warehouse in disabled_warehouses:
            frappe.db.set_value(
                "Warehouse",
                warehouse,
                "disabled",
                1,
                update_modified=False,
            )
