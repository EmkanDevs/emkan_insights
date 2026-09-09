import frappe
import json
from frappe.utils import flt, nowdate
from emkan_insights.emkan_insights.account_resolver import resolve_company_account
from emkan_insights.emkan_insights.registry.decorators import filter_kwargs
from emkan_insights.emkan_insights.external_sync_utils import (
    log_record_failure,
    run_chunks_fail_fast,
)
from emkan_insights.emkan_insights.sync_guard import (
    enable_supplier_for_sync,
    set_source_site_fields,
    sync_record_guard,
    temporarily_enable_transaction_items,
)
from emkan_insights.emkan_insights.uom_resolver import resolve_uom
from erpnext.controllers.status_updater import OverAllowanceError

# Chunk size used when the sequential pipeline runs this module synchronously.
CHUNK_SIZE = 300

# ==========================================================
# HELPERS
# ==========================================================
def sync_docstatus(doc, target_status):
    """
    Synchronise a Purchase Order's docstatus to match the source using the
    ORM submit()/cancel() path, which correctly runs all ERPNext lifecycle
    hooks (GL Entries, reserved quantities, linked document statuses, etc.).

    If ORM submit or cancel fails, the error is logged as CRITICAL and
    re-raised so the caller's savepoint rolls back only this record.
    Raw SQL docstatus flip is intentionally NOT used as a fallback.
    """
    target = int(target_status)
    current = int(frappe.db.get_value(doc.doctype, doc.name, "docstatus") or 0)

    if current == target:
        return

    try:
        doc.flags.ignore_permissions = True

        # Bypass payment-schedule validation during submit/cancel, matching the
        # same bypass applied in the save path (upsert_purchase_order). The
        # schedule copied from the source may sum to a different total than the
        # locally recomputed grand total (conversion-rate / tax-rounding
        # differences), which otherwise fails validate_payment_schedule_amount.
        # Purchase Orders carry no GL entries, so bypassing is safe.
        doc.validate_all_documents_schedule = lambda: None

        if target == 1:
            if current == 0:
                with temporarily_enable_transaction_items(doc):
                    doc.submit()  # ORM submit — runs all lifecycle hooks
            else:
                frappe.log_error(
                    title=f"PO {doc.name} unhandled docstatus transition: {current} → {target}",
                    message="Only Draft→Submitted and Draft/Submitted→Cancelled are handled."
                )

        elif target == 2:
            if current == 0:
                with temporarily_enable_transaction_items(doc):
                    doc.submit()  # Must submit first before cancel
                    doc.reload()
                    doc.cancel()  # ORM cancel — runs all lifecycle hooks
            else:
                with temporarily_enable_transaction_items(doc):
                    doc.cancel()  # ORM cancel — runs all lifecycle hooks

        elif target == 0:
            frappe.log_error(
                title=f"PO {doc.name} cannot revert to Draft",
                message=(
                    f"ERPNext does not support reverting a submitted/cancelled document "
                    f"to Draft via the ORM. Current docstatus: {current}. "
                    f"This is a data inconsistency on the source system."
                )
            )

    except OverAllowanceError:
        # MR qty overflow on submit — clear all MR links and retry once.
        frappe.log_error(
            title=f"PO {doc.name}: OverAllowanceError — clearing MR links and retrying submit",
            message=(
                f"Submit failed due to MR qty overflow. Clearing all material_request "
                f"links on PO items and retrying submit."
            ),
        )
        doc.reload()
        # ERPNext submit() sets docstatus=1 and then runs on_submit(). Here the
        # overflow error was raised inside on_submit (update_prevdoc_status →
        # validate_qty), so the doc is already Submitted in the DB even though
        # the MR qty was never consumed. detect that and clear the MR links
        # directly via frappe.db.set_value — the ORM save() path is blocked by
        # validate_update_after_submit once docstatus=1.
        current_docstatus = int(doc.docstatus or 0)
        if current_docstatus == 1:
            for item in doc.items:
                if item.material_request or item.material_request_item:
                    frappe.log_error(
                        title=f"PO {doc.name}: MR link cleared (qty overflow)",
                        message=(
                            f"Item {item.item_code}: clearing material_request={item.material_request}, "
                            f"material_request_item={item.material_request_item} after submit "
                            f"raised OverAllowanceError. The PO was left Submitted but no longer "
                            f"references the Material Request."
                        ),
                    )
                    frappe.db.set_value(
                        "Purchase Order Item",
                        item.name,
                        {"material_request": None, "material_request_item": None},
                    )
            return

        # Still Draft — clear MR links via ORM and retry submit.
        for item in doc.items:
            if item.material_request or item.material_request_item:
                frappe.log_error(
                    title=f"PO {doc.name}: MR link cleared (qty overflow)",
                    message=(
                        f"Item {item.item_code}: clearing material_request={item.material_request}, "
                        f"material_request_item={item.material_request_item} to prevent "
                        f"OverAllowanceError on submit."
                    ),
                )
                item.material_request = None
                item.material_request_item = None
        doc.save(ignore_permissions=True)

        with temporarily_enable_transaction_items(doc):
            doc.submit()

    except Exception as err:
        frappe.log_error(
            title=f"CRITICAL: PO {doc.name} ORM lifecycle failed ({current} → {target}) — manual intervention required",
            message=(
                f"Error: {str(err)}\n\n{frappe.get_traceback()}\n\n"
                f"The Purchase Order is stuck at docstatus={current}. "
                f"Investigate why submit()/cancel() failed and correct the document "
                f"manually via ERPNext."
            )
        )
        raise


def _force_docstatus_db(name, doctype, target_docstatus):
    """
    INTENTIONALLY DISABLED: Force-setting docstatus via raw SQL bypasses all
    ERPNext submit/cancel hooks (GL Entries, reserved quantities, linked
    document statuses, etc.).

    This stub is kept so any remaining call sites surface a clear error instead
    of silently corrupting data. Use sync_docstatus() (ORM path) instead.
    """
    raise frappe.ValidationError(
        f"_force_docstatus_db('{name}', '{doctype}', {target_docstatus}) is not allowed: "
        f"raw SQL docstatus changes bypass ERPNext lifecycle hooks. "
        f"Use the ORM submit()/cancel() path via sync_docstatus()."
    )


def resolve_account(account_head, company, company_abbr=None):
    """
    Resolve an account head to a valid account name in this company.
    Tries exact match first, then matches by the numeric code prefix
    (before the first space).
    """
    return resolve_company_account(account_head, company, company_abbr)


def _resolve_material_request(remote_mr_name, company_abbr=None):
    """
    Three-tier resolution, same pattern as RFQ sync:
    1. Prefixed local name: {company_abbr}-{remote_name}
    2. remote_id / custom_remote_id lookup
    3. Raw name existence check
    """
    if not remote_mr_name:
        return None

    if company_abbr and not remote_mr_name.startswith(f"{company_abbr}-"):
        prefixed = f"{company_abbr}-{remote_mr_name}"
        if frappe.db.exists("Material Request", prefixed):
            return prefixed

    for field in ("remote_id", "custom_remote_id"):
        if frappe.get_meta("Material Request").has_field(field):
            found = frappe.db.get_value("Material Request", {field: remote_mr_name}, "name")
            if found:
                return found

    if frappe.db.exists("Material Request", remote_mr_name):
        return remote_mr_name

    return None


def _resolve_link_with_abbr(doctype, remote_name, company_abbr=None):
    if not remote_name:
        return None

    candidates = [remote_name]
    if company_abbr and not remote_name.startswith(f"{company_abbr}-"):
        candidates.append(f"{company_abbr}-{remote_name}")
    elif company_abbr and remote_name.startswith(f"{company_abbr}-"):
        # Also try unprefixed remote_id (SQ stores custom_remote_id = PUR-SQTN-…)
        stripped = remote_name[len(f"{company_abbr}-"):]
        if stripped:
            candidates.append(stripped)

    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        if frappe.db.exists(doctype, name):
            return name

    meta = frappe.get_meta(doctype)
    for name in candidates:
        if not name:
            continue
        for field in ("remote_id", "custom_remote_id"):
            if meta.has_field(field):
                local_name = frappe.db.get_value(doctype, {field: name}, "name")
                if local_name:
                    return local_name

    # Bridge via External Supplier Quotation → synced SQ name
    if doctype == "Supplier Quotation":
        for name in candidates:
            if not name:
                continue
            if frappe.db.exists("External Supplier Quotation", name):
                linked = frappe.db.get_value(
                    "External Supplier Quotation", name, "remote_id"
                )
                if linked and frappe.db.exists("Supplier Quotation", linked):
                    return linked
            if meta.has_field("custom_remote_id"):
                local_name = frappe.db.get_value(
                    "Supplier Quotation", {"custom_remote_id": name}, "name"
                )
                if local_name:
                    return local_name
            if meta.has_field("remote_id"):
                local_name = frappe.db.get_value(
                    "Supplier Quotation", {"remote_id": name}, "name"
                )
                if local_name:
                    return local_name

    return None


def _resolve_supplier_quotation_item(sq_name, remote_item_id, item_code=None):
    """
    Map remote Supplier Quotation Item id → local child row name.
    External SQ sync stores the remote child id in custom_remote_id.
    """
    if not remote_item_id:
        return None

    # Already a local row name
    if sq_name and frappe.db.exists(
        "Supplier Quotation Item", {"name": remote_item_id, "parent": sq_name}
    ):
        return remote_item_id
    if frappe.db.exists("Supplier Quotation Item", remote_item_id):
        parent = frappe.db.get_value("Supplier Quotation Item", remote_item_id, "parent")
        if not sq_name or parent == sq_name:
            return remote_item_id

    filters = {"custom_remote_id": remote_item_id}
    if sq_name:
        filters["parent"] = sq_name

    if frappe.get_meta("Supplier Quotation Item").has_field("custom_remote_id"):
        found = frappe.db.get_value("Supplier Quotation Item", filters, "name")
        if found:
            return found

    # Fallback: match by item_code under the resolved SQ parent
    if sq_name and item_code:
        found = frappe.db.get_value(
            "Supplier Quotation Item",
            {"parent": sq_name, "item_code": item_code},
            "name",
        )
        if found:
            return found

    return None


def _check_mr_overflow(mr_name, mr_item_name, qty, item_code=None, po_name=None):
    """
    Check if linking this item to the Material Request would cause an
    OverAllowanceError on submit. Returns True if overflow would occur.
    """
    if not mr_name or not mr_item_name:
        return False

    mr_item_row = frappe.db.get_value(
        "Material Request Item", mr_item_name,
        ["stock_qty", "parent"], as_dict=True
    )
    if not mr_item_row:
        return False

    mr_stock_qty = flt(mr_item_row.stock_qty)

    # Allowance: per-item fallback to Stock Settings (default 0%)
    allowance_pct = 0.0
    if item_code:
        allowance_pct = flt(frappe.db.get_value("Item", item_code, "over_delivery_receipt_allowance") or 0)
    if not allowance_pct:
        allowance_pct = flt(
            frappe.db.get_single_value("Stock Settings", "over_delivery_receipt_allowance") or 0
        )
    allowed_qty = mr_stock_qty * (1 + allowance_pct / 100.0)

    # Sum of all OTHER POs already linked to this MR item (exclude current PO)
    existing_qty = frappe.db.sql("""
        SELECT COALESCE(SUM(po_item.stock_qty), 0)
        FROM `tabPurchase Order Item` po_item
        JOIN `tabPurchase Order` po ON po_item.parent = po.name
        WHERE po_item.material_request_item = %s
          AND po.docstatus = 1
          AND po.name != %s
    """, (mr_item_name, po_name or ""), as_dict=False)[0][0]

    total_qty = flt(existing_qty) + flt(qty)

    if total_qty > allowed_qty:
        frappe.log_error(
            title=f"PO {po_name or 'new'}: MR link cleared (qty overflow)",
            message=(
                f"Material Request Item {mr_item_name}: MR stock_qty={mr_stock_qty}, "
                f"existing PO qty={existing_qty}, this PO qty={qty}, "
                f"total={total_qty} > allowed={allowed_qty} "
                f"(allowance={allowance_pct}%). Clearing MR link to prevent "
                f"OverAllowanceError on submit."
            ),
        )
        return True

    return False


def _build_target_name(company_abbr, remote_id):
    if company_abbr and remote_id.startswith(f"{company_abbr}-"):
        return remote_id
    return f"{company_abbr}-{remote_id}" if company_abbr else remote_id


def _find_existing_po(remote_id, target_name):
    """
    Check by remote_id first (authoritative link back to source),
    then fall back to the prefixed target name.
    """
    existing = frappe.db.get_value("Purchase Order", {"remote_id": remote_id}, "name")
    if existing:
        return existing
    if frappe.db.exists("Purchase Order", target_name):
        return target_name
    return None


def _get_buying_price_list(src):
    if src.buying_price_list and frappe.db.exists("Price List", src.buying_price_list):
        return src.buying_price_list
    return None


def _apply_po_header_fields(doc, src, company_abbr, conversion_rate):
    doc.update({
        "company": src.company,
        "supplier": src.supplier,
        "supplier_name": src.supplier_name,
        "transaction_date": src.transaction_date or nowdate(),
        "schedule_date": src.schedule_date or src.transaction_date or nowdate(),
        "currency": src.currency or "SAR",
        "conversion_rate": round(conversion_rate, 2),
        "plc_conversion_rate": flt(round(src.plc_conversion_rate),2) or 1.0,
        "remote_id": src.remote_id,
        "buying_price_list": _get_buying_price_list(src),
        "apply_discount_on": src.apply_discount_on or "Net Total",
        "disable_rounded_total": src.disable_rounded_total or 0,
        "taxes_and_charges": None,
        # Copy the source payment terms template so the synced PO preserves
        # the payment terms agreed on the External PO. Only set it when it
        # resolves to a real Payment Terms Template on this system; otherwise
        # leave it empty so ERPNext does not fall back to the supplier's
        # default terms and silently generate a schedule we did not ask for.
        #
        # The payment_schedule child rows are rebuilt explicitly from the
        # source (see _insert_po_payment_schedule) rather than relying on
        # the template to regenerate them. Duplicate due dates are handled by
        # _dedup_payment_schedule() and the validate_all_documents_schedule
        # bypass applied on save.
        "payment_terms_template": (
            src.payment_terms_template
            if getattr(src, "payment_terms_template", None)
            and frappe.db.exists("Payment Terms Template", src.payment_terms_template)
            else None
        ),
        "payment_terms": None,
        # Copy terms and conditions. tc_name (Link to Terms and Conditions) is
        # only set when it resolves to a real record on this system; the
        # free-text terms body is copied directly.
        "tc_name": (
            src.tc_name
            if getattr(src, "tc_name", None)
            and frappe.db.exists("Terms and Conditions", src.tc_name)
            else None
        ),
        "terms": getattr(src, "terms", None) or "",
    })

    src_site_value = getattr(src, "site_url", None) or getattr(src, "source_site", None)
    if src_site_value:
        set_source_site_fields(doc, site_url=src_site_value)

    doc.total = flt(src.total)
    doc.net_total = flt(src.net_total) or flt(doc.total)
    doc.base_total = flt(src.base_total)
    doc.base_net_total = flt(src.base_net_total) or flt(doc.base_total)
    doc.total_qty = flt(src.total_qty)
    doc.grand_total = flt(src.grand_total)
    doc.base_grand_total = flt(src.base_grand_total)
    doc.base_taxes_and_charges_added = flt(src.base_taxes_and_charges_added)
    doc.taxes_and_charges_added = flt(src.taxes_and_charges_added)
    doc.base_taxes_and_charges_deducted = flt(src.base_taxes_and_charges_deducted)
    doc.taxes_and_charges_deducted = flt(src.taxes_and_charges_deducted)
    doc.total_taxes_and_charges = flt(src.total_taxes_and_charges)
    doc.base_total_taxes_and_charges = flt(src.base_total_taxes_and_charges)
    doc.tax_withholding_net_total = flt(getattr(src, "tax_withholding_net_total", 0))
    doc.base_tax_withholding_net_total = flt(getattr(src, "base_tax_withholding_net_total", 0))
    doc.rounding_adjustment = flt(src.rounding_adjustment)
    doc.base_rounding_adjustment = flt(src.base_rounding_adjustment)
    doc.rounded_total = flt(src.rounded_total)
    doc.base_rounded_total = flt(src.base_rounded_total)
    doc.in_words = getattr(src, "in_words", "")
    doc.base_in_words = getattr(src, "base_in_words", "")


def _prepare_existing_draft_po(src, existing_name, company_abbr, conversion_rate):
    doc = frappe.get_doc("Purchase Order", existing_name)
    current_docstatus = int(doc.docstatus or 0)

    if current_docstatus == 2:
        frappe.throw(
            f"Purchase Order {existing_name} is already cancelled. "
            "Amend it through ERPNext before syncing again.",
            exc=frappe.ValidationError,
        )

    if current_docstatus == 1:
        # Cancel the submitted PO via ORM so all lifecycle hooks run, then
        # create an amendment draft. The amendment carries the same name suffix
        # that ERPNext appends (-1, -2, …) and starts in Draft state.
        doc.flags.ignore_permissions = True
        with temporarily_enable_transaction_items(doc):
            doc.cancel()
        doc.reload()

        amendment = frappe.copy_doc(doc)
        amendment.amended_from = doc.name
        amendment.docstatus = 0
        amendment.flags.ignore_permissions = True
        amendment.flags.ignore_mandatory = True
        amendment.flags.ignore_links = True
        amendment.set("items", [])
        amendment.set("taxes", [])
        amendment.set("payment_schedule", [])
        _apply_po_header_fields(amendment, src, company_abbr, conversion_rate)
        amendment.validate_all_documents_schedule = lambda: None
        amendment.insert(ignore_permissions=True)

        frappe.log_error(
            title=f"PO Sync: auto cancel-and-amend {existing_name} → {amendment.name}",
            message=(
                f"Source ExternalPurchaseOrder {src.name} had updated data after the PO "
                f"was submitted. Cancelled {existing_name} and created amendment {amendment.name}."
            ),
        )
        return amendment.name

    # Draft PO — delete all existing child rows so the ORM save starts from a
    # clean slate. If we only set([]) and save(), any TypeError/crash in save
    # leaves the old rows in the DB and the next insert doubles them.
    for child_dt in ("Purchase Order Item", "Purchase Taxes and Charges", "Payment Schedule"):
        frappe.db.delete(child_dt, {"parent": existing_name, "parenttype": "Purchase Order"})

    # ignore_mandatory must be set as a flag — _save() does not accept it as a kwarg.
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_links = True
    _apply_po_header_fields(doc, src, company_abbr, conversion_rate)
    doc.set("items", [])
    doc.set("taxes", [])
    doc.set("payment_schedule", [])
    # Bypass payment-schedule validation on this intermediate save. Now that
    # payment_terms_template is copied from the source, ERPNext's validate()
    # regenerates the schedule here and can produce duplicate due dates. The
    # schedule is rebuilt from source and deduped later in upsert_purchase_order,
    # so this intermediate save only needs the header/parent to persist.
    doc.validate_all_documents_schedule = lambda: None
    doc.save(ignore_permissions=True)
    return doc.name



def _dedup_payment_schedule(doc):
    """
    Remove payment-schedule rows that share a due_date, keeping the one with
    the highest payment_amount (or the first occurrence when amounts are equal).

    ERPNext's validate_payment_schedule_dates() throws a ValidationError when
    two rows carry the same due_date.  This can happen when the supplier's
    payment-terms template itself contains duplicate dates, or when ERPNext’s
    onload()/set_payment_schedule() appends a row that was already present.

    A PO carries no GL entries, so dropping duplicate schedule rows is safe
    and has no accounting impact.
    """
    seen_dates = {}
    for row in doc.get("payment_schedule"):
        d = str(row.due_date or "")
        if d not in seen_dates:
            seen_dates[d] = row
        else:
            # Keep whichever row has the larger payment amount.
            existing = seen_dates[d]
            if flt(row.payment_amount) > flt(existing.payment_amount):
                seen_dates[d] = row

    kept = list(seen_dates.values())
    if len(kept) != len(doc.get("payment_schedule")):
        frappe.log_error(
            title=f"PO Sync: deduped payment_schedule on {doc.name or 'new'}",
            message=(
                f"Original rows: {len(doc.get('payment_schedule'))}, "
                f"after dedup: {len(kept)}. "
                "Removed rows had duplicate due_dates from payment terms template."
            ),
        )
    doc.set("payment_schedule", kept)


def _insert_new_po(src, target_name, company_abbr, conversion_rate):
    doc = frappe.new_doc("Purchase Order")
    doc.name = target_name
    doc.flags.name_set = True
    # ignore_mandatory must be set as a flag — insert() does not accept it as a kwarg.
    doc.flags.ignore_mandatory = True
    _apply_po_header_fields(doc, src, company_abbr, conversion_rate)
    doc.set("payment_schedule", [])
    # Bypass all payment-schedule validation that runs inside validate().
    # validate_all_documents_schedule covers three checks: duplicate due dates,
    # amount-vs-grand-total, and due-date ordering. All three can fail when:
    #   - the supplier's payment-terms template has duplicate dates, or
    #   - the shell insert has grand_total=0 (no items yet) so the
    #     auto-generated schedule row carries payment_amount=0.
    # Purchase Orders carry no GL entries, so bypassing these checks is safe.
    doc.validate_all_documents_schedule = lambda: None
    doc.insert(ignore_permissions=True)
    return doc.name


# ==========================================================
# SAFE CHILD INSERTION (dedup by source row identity, not content)
# ==========================================================

def _insert_po_items_safely(po_doc, src, company_abbr=None):
    """
    Append Purchase Order Items via the ORM (po_doc.append) so ERPNext runs
    validate() and computes base_* fields correctly on save().

    *po_doc* is the Purchase Order document instance that items are appended
    to — the caller is responsible for calling po_doc.save() afterwards.

    Dedup uses the SOURCE ROW'S identity (row.name / custom_remote_id),
    never (item_code, qty, rate) - that fingerprint incorrectly collapses
    legitimate repeated line items (e.g. same item ordered 3x separately).

    Errors propagate immediately — no silent skipping.
    """
    if not src.items:
        return 0, []

    success_count = 0
    seen_source_rows = set()
    seen_mr_items = set()
    inserted_map = []

    po_name = po_doc.name

    for idx, row in enumerate(src.items, start=1):
        row_identifier = getattr(row, "custom_remote_id", None) or row.name
        if row_identifier in seen_source_rows:
            frappe.log_error(
                title=f"PO {po_name} duplicate item skipped",
                message=f"Source row {row_identifier} (idx {idx}) already processed "
                        f"in this sync run - duplicate data in External PO source."
            )
            continue
        seen_source_rows.add(row_identifier)

        qty = flt(row.qty)
        rate = flt(row.rate)
        amount = flt(row.amount) if flt(row.amount) else qty * rate

        mr_name = _resolve_material_request(getattr(row, "material_request", None), company_abbr)
        mr_item_name = None
        if mr_name:
            # Drop the link if the MR is cancelled — ERPNext raises
            # CancelledLinkError on submit when any linked doc is docstatus=2.
            mr_docstatus = frappe.db.get_value("Material Request", mr_name, "docstatus")
            if frappe.utils.cint(mr_docstatus) == 2:
                frappe.log_error(
                    title=f"PO {po_name}: MR link cleared (MR cancelled)",
                    message=(
                        f"Row {idx} (item: {row.item_code}): Material Request '{mr_name}' "
                        f"is cancelled (docstatus=2). Clearing the link so the PO "
                        f"can be submitted without CancelledLinkError."
                    ),
                )
                mr_name = None
            else:
                # ERPNext compares MR.company against PO.company in
                # validate_with_previous_doc — drop the link if they differ.
                mr_company = frappe.db.get_value("Material Request", mr_name, "company")
                if po_doc.company and mr_company and mr_company != po_doc.company:
                    frappe.log_error(
                        title=f"PO {po_name}: MR link cleared (company mismatch)",
                        message=(
                            f"Row {idx} (item: {row.item_code}): Material Request '{mr_name}' "
                            f"company '{mr_company}' != PO company '{po_doc.company}'. "
                            f"Clearing the link so validate_with_previous_doc passes."
                        ),
                    )
                    mr_name = None
                else:
                    mr_item_name = frappe.db.get_value(
                        "Material Request Item",
                        {"parent": mr_name, "item_code": row.item_code},
                        "name"
                    )
                if mr_item_name in seen_mr_items:
                    frappe.log_error(
                        title=f"PO {po_name} duplicate MR item link cleared",
                        message=(
                            f"Row {idx} (item: {row.item_code}) references Material Request Item "
                            f"{mr_item_name} which is already linked by an earlier row. "
                            f"Cleared mr link on this row to prevent submit validation error."
                        ),
                    )
                    mr_name = None
                    mr_item_name = None
                else:
                    seen_mr_items.add(mr_item_name)

                    # Drop MR link if cumulative qty would overflow on submit
                    if _check_mr_overflow(mr_name, mr_item_name, qty, item_code=row.item_code, po_name=po_name):
                        mr_name = None
                        mr_item_name = None

        sq_name = _resolve_link_with_abbr(
            "Supplier Quotation",
            getattr(row, "supplier_quotation", None),
            company_abbr,
        )
        sq_item_name = _resolve_supplier_quotation_item(
            sq_name,
            getattr(row, "supplier_quotation_item", None),
            item_code=row.item_code,
        )
        if sq_item_name:
            sq_fields = frappe.db.get_value(
                "Supplier Quotation Item",
                sq_item_name,
                ["rate", "uom", "item_code", "conversion_factor"],
                as_dict=True,
            ) or {}

            sq_header = frappe.db.get_value(
                "Supplier Quotation",
                sq_name,
                ["supplier", "company", "currency"],
                as_dict=True,
            ) or {}

            sq_rate = flt(sq_fields.get("rate"))
            sq_uom = resolve_uom(sq_fields.get("uom")) if sq_fields.get("uom") else None
            sq_item_code = sq_fields.get("item_code")
            sq_cf = flt(sq_fields.get("conversion_factor") or 1.0)
            row_cf = flt(getattr(row, "conversion_factor", None) or 1.0)
            resolved_row_uom = resolve_uom(row.uom) if row.uom else None

            mismatch_reason = None
            if sq_rate != rate:
                mismatch_reason = f"rate {rate} != SQ rate {sq_rate}"
            elif sq_uom and resolved_row_uom and sq_uom != resolved_row_uom:
                mismatch_reason = f"uom '{row.uom}' (resolved: '{resolved_row_uom}') != SQ uom '{sq_uom}'"
            elif sq_item_code and row.item_code and sq_item_code != row.item_code:
                mismatch_reason = f"item_code '{row.item_code}' != SQ item_code '{sq_item_code}'"
            elif sq_cf and row_cf and sq_cf != row_cf:
                mismatch_reason = f"conversion_factor {row_cf} != SQ conversion_factor {sq_cf}"

            if mismatch_reason:
                frappe.log_error(
                    title=f"PO {po_name} SQ link dropped",
                    message=(
                        f"External PO row {row.name}: {mismatch_reason}. "
                        f"Cleared supplier_quotation links so PO can preserve the "
                        f"source PO values without failing submit validation."
                    ),
                )
                sq_name = None
                sq_item_name = None

            elif sq_item_name and sq_uom:
                raw_sq_uom = sq_fields.get("uom")
                if raw_sq_uom and raw_sq_uom != sq_uom:
                    frappe.db.set_value(
                        "Supplier Quotation Item",
                        sq_item_name,
                        "uom",
                        sq_uom,
                        update_modified=False,
                    )
                    frappe.log_error(
                        title=f"SQ item UOM normalised ({sq_item_name})",
                        message=(
                            f"Supplier Quotation Item {sq_item_name} had UOM '{raw_sq_uom}' "
                            f"in the database; updated to canonical form '{sq_uom}' so "
                            f"PO {po_name} validate_with_previous_doc passes."
                        ),
                    )

                # A PO linked to a Supplier Quotation must share its
                # supplier/company/currency (ERPNext validate_with_previous_doc
                # compares the PO header against the SQ). Inherit these from the
                # reference doc so the PO is internally consistent instead of
                # having its link silently dropped.
                if sq_header.get("supplier") and po_doc.supplier != sq_header["supplier"]:
                    po_doc.supplier = sq_header["supplier"]
                if sq_header.get("company") and po_doc.company != sq_header["company"]:
                    po_doc.company = sq_header["company"]
                if sq_header.get("currency") and po_doc.currency != sq_header["currency"]:
                    po_doc.currency = sq_header["currency"]

        resolved_uom = resolve_uom(row.uom)
        resolved_stock_uom = resolve_uom(
            getattr(row, "stock_uom", None) or row.uom
        )
        conversion_factor = flt(getattr(row, "conversion_factor", None) or 1.0)

        child = po_doc.append("items", {
            "item_code": row.item_code,
            "item_name": row.item_name or row.item_code,
            "description": getattr(row, "description", None) or row.item_name or row.item_code,
            "custom_remote_id": getattr(row, "custom_remote_id", None) or row.name,
            "qty": qty,
            "stock_qty": qty * conversion_factor,
            "rate": rate,
            "amount": amount,
            "net_rate": flt(row.net_rate) if flt(getattr(row, "net_rate", 0)) else rate,
            "net_amount": flt(row.net_amount) if flt(getattr(row, "net_amount", 0)) else amount,
            "uom": resolved_uom,
            "stock_uom": resolved_stock_uom,
            "conversion_factor": conversion_factor,
            "warehouse": getattr(row, "warehouse", None),
            "schedule_date": row.schedule_date or po_doc.schedule_date or nowdate(),
            "project": getattr(row, "project", None) or getattr(src, "project", None),
            "cost_center": getattr(row, "cost_center", None),
            "price_list_rate": flt(getattr(row, "price_list_rate", 0)),
            "discount_percentage": flt(getattr(row, "discount_percentage", 0)),
            "discount_amount": flt(getattr(row, "discount_amount", 0)),
            "item_tax_rate": getattr(row, "item_tax_rate", "{}") or "{}",
            "against_blanket_order": 0,
            "blanket_order": None,
            "blanket_order_rate": 0,
            "apply_tds": getattr(row, "apply_tds", 0),
            "sales_order": _resolve_link_with_abbr("Sales Order", getattr(row, "sales_order", None), company_abbr),
            "sales_order_item": getattr(row, "sales_order_item", None),
            "supplier_quotation": sq_name,
            "supplier_quotation_item": sq_item_name,
            "material_request": mr_name,
            "material_request_item": mr_item_name,
            "expense_account": resolve_account(
                getattr(row, "expense_account", None), src.company, company_abbr
            ),
        })

        success_count += 1
        inserted_map.append((row.name, child.name))

    return success_count, inserted_map


def _insert_po_taxes_safely(po_doc, src, company_abbr=None):
    """
    Append Purchase Taxes and Charges via the ORM so ERPNext runs validate().
    *po_doc* is the same Purchase Order document instance items were appended
    to — the caller is responsible for calling po_doc.save() afterwards.

    Do NOT set base_tax_amount or base_total — ERPNext computes base fields
    from the parent's conversion_rate on save().

    Errors propagate immediately.
    """
    if not src.taxes:
        return 0

    success_count = 0
    seen_tax_rows = set()

    for idx, tax in enumerate(src.taxes, start=1):
        tax_key = (
            getattr(tax, "charge_type", None) or "On Net Total",
            getattr(tax, "account_head", None),
            flt(getattr(tax, "rate", 0)),
            flt(getattr(tax, "tax_amount", 0)),
            getattr(tax, "add_deduct_tax", None) or "Add",
            getattr(tax, "category", None) or "Total",
        )
        if tax_key in seen_tax_rows:
            continue
        seen_tax_rows.add(tax_key)

        acc = resolve_account(tax.account_head, src.company, company_abbr)
        if not acc:
            frappe.throw(
                f"Tax row {idx}: account '{tax.account_head}' not found for company {src.company}",
                exc=frappe.ValidationError,
            )

        tax_amount = flt(tax.tax_amount)

        po_doc.append("taxes", {
            "charge_type": tax.charge_type or "On Net Total",
            "account_head": acc,
            "description": tax.description or acc,
            "rate": flt(tax.rate),
            "tax_amount": tax_amount,
            "tax_amount_after_discount_amount": flt(getattr(tax, "tax_amount_after_discount_amount", 0)) or tax_amount,
            "total": flt(getattr(tax, "total", 0)),
            "add_deduct_tax": tax.add_deduct_tax or "Add",
            "category": tax.category or "Total",
            "included_in_print_rate": getattr(tax, "included_in_print_rate", 0),
            "included_in_paid_amount": getattr(tax, "included_in_paid_amount", 0),
            "item_wise_tax_detail": getattr(tax, "item_wise_tax_detail", "{}") or "{}",
            "cost_center": getattr(tax, "cost_center", None),
        })

        success_count += 1

    return success_count


def _insert_po_payment_schedule(po_doc, src):
    """
    Rebuild the Purchase Order's Payment Schedule from the source External PO.

    Rows are appended via the ORM with fresh idx values and no system fields,
    mirroring the Sales Order sync's approach. Only payment-relevant fields are
    copied so stale/derived columns from the source doc are not carried over.

    Callers must run _dedup_payment_schedule() afterwards (they already do in
    the save path) to guard against duplicate due dates.
    """
    if not getattr(src, "payment_schedule", None):
        return 0

    po_doc.set("payment_schedule", [])
    for idx, row in enumerate(src.payment_schedule, start=1):
        po_doc.append("payment_schedule", {
            "doctype": "Payment Schedule",
            "idx": idx,
            "payment_term": getattr(row, "payment_term", None),
            "description": getattr(row, "description", None),
            "due_date": getattr(row, "due_date", None),
            "invoice_portion": flt(getattr(row, "invoice_portion", 0)),
            "discount": flt(getattr(row, "discount", 0)),
            "payment_amount": flt(getattr(row, "payment_amount", 0)),
            "base_payment_amount": flt(getattr(row, "base_payment_amount", 0)),
        })

    return len(po_doc.get("payment_schedule"))


# ==========================================================
# MAIN UPSERT
# ==========================================================

def upsert_purchase_order(src, existing_name):
    """
    Build or update a Purchase Order from external source data using the ORM
    path throughout — no raw child-table inserts.  ERPNext's validate() and
    calculate logic run on save(), so base_* fields and UOM conversion are
    computed correctly without manual arithmetic.

    Every call runs inside the same open transaction; the caller
    (sync_bulk_purchase_orders) decides when this record's work becomes
    durable.
    """
    enable_supplier_for_sync(src.supplier)

    company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""
    conversion_rate = flt(src.conversion_rate) or 1.0
    target_name = _build_target_name(company_abbr, src.remote_id or src.name)

    if existing_name:
        po_name = _prepare_existing_draft_po(
            src,
            existing_name,
            company_abbr,
            conversion_rate,
        )
    else:
        po_name = _insert_new_po(src, target_name, company_abbr, conversion_rate)

    # ----------------------------------------------------------
    # Append children via ORM onto a single doc instance so
    # ERPNext validates everything together on save().
    # ----------------------------------------------------------
    po_doc = frappe.get_doc("Purchase Order", po_name)
    po_doc.flags.ignore_permissions = True
    po_doc.flags.ignore_mandatory = True
    po_doc.flags.ignore_links = True

    item_success, inserted_map = _insert_po_items_safely(
        po_doc, src, company_abbr,
    )

    tax_success = _insert_po_taxes_safely(
        po_doc, src, company_abbr,
    )

    schedule_success = _insert_po_payment_schedule(
        po_doc, src,
    )

    # ----------------------------------------------------------
    # Save the PO through the ORM so ERPNext runs validate() on
    # all child rows, computes base_* fields, and enforces
    # linked-document checks.  Disabled items are temporarily
    # enabled so they do not block validation — they are
    # re-disabled immediately after.
    # ----------------------------------------------------------
    # Bypass all payment-schedule validation that runs inside validate().
    # validate_all_documents_schedule covers three checks: duplicate due dates,
    # amount-vs-grand-total, and due-date ordering. The auto-generated payment
    # schedule may not match the PO's grand total or may contain duplicate
    # dates from the supplier's payment-terms template. Purchase Orders carry
    # no GL entries, so bypassing these checks is safe.
    _dedup_payment_schedule(po_doc)
    po_doc.validate_all_documents_schedule = lambda: None
    with temporarily_enable_transaction_items(po_doc):
        po_doc.save(ignore_permissions=True)

    # ----------------------------------------------------------
    # Verify counts
    # ----------------------------------------------------------
    actual_items = len(po_doc.items)
    actual_taxes = len(po_doc.taxes)
    expected_items = len(src.items or [])
    expected_taxes = len(src.taxes or [])

    if actual_items != expected_items:
        frappe.log_error(
            title=f"PO Sync items mismatch: {po_name}",
            message=f"Expected {expected_items} items, found {actual_items}. "
                    f"Successfully appended: {item_success}. "
                    f"This may indicate duplicate source rows in External PO - "
                    f"check the External PO's fetch/refetch logic, not this sync."
        )

    if actual_taxes != expected_taxes:
        frappe.log_error(
            title=f"PO Sync taxes mismatch: {po_name}",
            message=f"Expected {expected_taxes} taxes, found {actual_taxes}."
        )

    expected_schedule = len(src.payment_schedule or [])
    if schedule_success != expected_schedule:
        frappe.log_error(
            title=f"PO Sync payment_schedule mismatch: {po_name}",
            message=f"Expected {expected_schedule} schedule rows, found {schedule_success}."
        )

    # ----------------------------------------------------------
    # Write back custom_remote_id onto the SOURCE (External PO) items,
    # so future refetches/syncs of this same row are traceable both ways.
    # ----------------------------------------------------------
    if frappe.get_meta("Purchase Order Item").has_field("custom_remote_id"):
        has_source_child = frappe.db.exists("DocType", "External Purchase Order Item")
        source_item_meta = frappe.get_meta("External Purchase Order Item") if has_source_child else None
        if has_source_child and source_item_meta.has_field("custom_remote_id"):
            for source_row_name, new_child_name in inserted_map:
                # only write if the External PO item row doesn't already carry it
                current = frappe.db.get_value("External Purchase Order Item", source_row_name, "custom_remote_id")
                if not current:
                    frappe.db.set_value(
                        "External Purchase Order Item", source_row_name,
                        "custom_remote_id", new_child_name,
                        update_modified=False
                    )

    # ----------------------------------------------------------
    # Write back remote_id onto the External PO parent
    # ----------------------------------------------------------
    if frappe.get_meta("External Purchase Order").has_field("remote_id"):
        frappe.db.set_value("External Purchase Order", src.name, "remote_id", po_name, update_modified=False)

    # ----------------------------------------------------------
    # Reload and sync docstatus
    # ----------------------------------------------------------
    frappe.clear_document_cache("Purchase Order", po_name)
    doc = frappe.get_doc("Purchase Order", po_name)
    sync_docstatus(doc, src.docstatus)

    return doc


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_purchase_order_docs(source_doctype: str, names, sync_now=False):
    if isinstance(names, str):
        names = json.loads(names)

    # If no names passed, fetch unsynced
    if not names:
        names = frappe.db.sql(f"""
            SELECT name
            FROM `tab{source_doctype}`
            WHERE
                last_synced_on IS NULL
                OR modified >= last_synced_on
        """, pluck=True)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    if sync_now:
        # Sequential pipeline path: strict fail-fast. Each chunk is completed
        # before the next starts; the first record failure aborts everything.
        return run_chunks_fail_fast(
            unique_names,
            CHUNK_SIZE,
            lambda chunk: sync_bulk_purchase_orders(names=chunk),
        )

    # Enqueue as a single background job — sequential processing.
    # No chunking: POs must sync serially so linked-document state is
    # consistent and errors surface immediately without race conditions.
    frappe.enqueue(
        "emkan_insights.emkan_insights.external_po_sync.sync_bulk_purchase_orders",
        queue="long",
        job_name=f"po_sync_{frappe.generate_hash(length=8)}",
        timeout=6000,
        names=unique_names,
    )

    return f"Sync Queued: {len(unique_names)} record(s)"


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def sync_bulk_purchase_orders(names):
    """
    Runs as a background job. No explicit commits anywhere in this loop -
    Frappe's job runner commits automatically once this function returns
    without an uncaught exception (and rolls back the whole job if one
    does). Each External PO gets its own savepoint. Strictly fail-fast:
    the first record failure is logged and re-raised so the sync (job or
    sequential pipeline) terminates as failed and the record stays
    retryable.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []

    for name in names:
        try:
            with sync_record_guard("External Purchase Order", name):
                src = frappe.get_doc("External Purchase Order", name)
                sync_started_at = src.modified

                company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""
                target_name = _build_target_name(company_abbr, src.remote_id or src.name)
                existing = _find_existing_po(src.remote_id or src.name, target_name)

                doc = upsert_purchase_order(src, existing)
                src.db_set(
                    "last_synced_on",
                    sync_started_at,
                    update_modified=False
                )

                results.append({
                    "name": name,
                    "status": "success",
                    "po_name": doc.name,
                    "items": len(doc.items),
                    "taxes": len(doc.taxes),
                    "docstatus": doc.docstatus
                })

        except Exception as e:
            # sync_record_guard has already rolled this record back to its
            # savepoint. Log the failure centrally and re-raise so the
            # pipeline (or the background job) terminates as failed.
            log_record_failure("Purchase Order", name, str(e))
            raise

    return results


