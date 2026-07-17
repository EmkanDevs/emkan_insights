import frappe
import json


SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "__last_sync_on", "amended_from",
    "amendment_date", "_comments", "_liked_by", "_assign", "_seen"
}

CHILD_TABLES = ["items", "taxes", "payment_schedule"]

# payment_terms_template is what silently repopulates payment_schedule via
# set_missing_values() AFTER we've explicitly cleared it below - excluding
# it here (in addition to CHILD_TABLES) stops that regeneration from ever
# being triggered in the first place, which is the actual root cause of
# the "duplicate due dates" validation error.
EXPLICITLY_HANDLED_HEADER_FIELDS = {"payment_terms_template", "party_name", "quotation_to"}

# These fields reference records (Address / Contact) that only make sense
# in the context of the *target* system's party. Copying them blindly from
# the source doc regularly fails Frappe's own validation ("Contact Person
# does not belong to ...", "Address ... not found") and aborts the whole
# record. We validate them ourselves before setting so a bad value just
# results in an empty field instead of a failed sync.
ADDRESS_FIELDS_TO_VALIDATE = {"customer_address", "shipping_address_name"}
CONTACT_FIELDS_TO_VALIDATE = {"contact_person"}
SOFT_VALIDATED_HEADER_FIELDS = ADDRESS_FIELDS_TO_VALIDATE | CONTACT_FIELDS_TO_VALIDATE


# ==========================================================
# ENTRY POINT
# ==========================================================

@frappe.whitelist()
def sync_external_quotation_docs(source_doctype, names):

    if isinstance(names, str):
        names = json.loads(names)

    created = []
    updated = []
    failed = []

    for name in names:
        try:
            src = frappe.get_doc(source_doctype, name)

            remote_id = src.name
            company_abbr = frappe.db.get_value('Company', src.company, 'abbr') or ''

            if not company_abbr:
                frappe.log_error(
                    f"Quotation Sync: no company abbr found for company "
                    f"'{src.company}' on {remote_id}. Name will be created "
                    f"without a company prefix.",
                    "ExternalQuotation Sync - missing company abbr"
                )

            target_name = _build_target_name(company_abbr, remote_id)

            existing = frappe.get_value("Quotation", {"remote_id": remote_id}, "name")
            if not existing and frappe.db.exists("Quotation", target_name):
                existing = target_name

            doc = upsert_quotation(src, target_name, existing)
            sync_docstatus(doc, src.docstatus)

            # Commit per record so a later failure in the batch doesn't
            # roll back records that already succeeded.
            frappe.db.commit()

            if existing:
                updated.append(doc.name)
            else:
                created.append(doc.name)

        except Exception:
            frappe.db.rollback()
            error = frappe.get_traceback()
            frappe.log_error(title=f"Quotation Sync Error: {name}", message=error)
            failed.append({"name": name, "error": error})

    return {
        "created": created,
        "updated": updated,
        "failed": failed
    }


# ==========================================================
# FIELD VALIDATION HELPERS
# ==========================================================

def _is_address_valid(value):
    """True if the Address record exists in the target system."""
    if not value:
        return False
    return bool(frappe.db.exists("Address", value))


def _is_contact_valid_for_party(value, party_type, party_name):
    """
    True if the Contact record exists AND is actually linked to the
    quotation's party (via the Dynamic Link child table), which is what
    Frappe's own contact_person validation checks. Mirrors the check
    Frappe does internally so we can pre-empt the failure instead of
    catching it after the fact.
    """
    if not value or not party_type or not party_name:
        return False

    if not frappe.db.exists("Contact", value):
        return False

    return bool(frappe.db.exists("Dynamic Link", {
        "parenttype": "Contact",
        "parent": value,
        "link_doctype": party_type,
        "link_name": party_name
    }))


def _build_target_name(company_abbr, remote_id):
    remote_id = (remote_id or "").strip()
    if not remote_id:
        return None

    if not company_abbr or remote_id.startswith(f"{company_abbr}-"):
        return remote_id

    return f"{company_abbr}-{remote_id}"


def _resolve_company_prefixed_link(doctype, value, company_abbr):
    value = (value or "").strip()
    if not value:
        return value

    candidates = [value]
    if company_abbr and not value.startswith(f"{company_abbr}-"):
        candidates.append(f"{company_abbr}-{value}")

    fieldnames = {df.fieldname for df in frappe.get_meta(doctype).fields}
    for candidate in candidates:
        if frappe.db.exists(doctype, candidate):
            return candidate

        for remote_field in ("custom_remote_id", "remote_id"):
            if remote_field not in fieldnames:
                continue

            linked_name = frappe.db.get_value(doctype, {remote_field: candidate}, "name")
            if linked_name:
                return linked_name

    return value


# ==========================================================
# UPSERT (SAFE)
# ==========================================================

def upsert_quotation(src, target_name, existing_name=None):
    if existing_name:
        quotation = frappe.get_doc("Quotation", existing_name)

        # Already submitted/cancelled - don't touch financials from a bulk
        # sync. Skip update, return as-is (this is intentional, not a bug -
        # it shows up as "unchanged", not "missing").
        if quotation.docstatus > 0:
            return quotation
    else:
        quotation = frappe.new_doc("Quotation")
        quotation.name = target_name
        quotation.flags.name_set = True
        quotation.flags.ignore_naming_series = True

    # --------------------------------------------------
    # REQUIRED FIELDS
    # --------------------------------------------------
    company_abbr = frappe.db.get_value("Company", src.company, "abbr") or ""

    quotation.remote_id = src.name
    quotation.company = src.company
    quotation.quotation_to = src.quotation_to or "Customer"
    resolved_party_name = _resolve_company_prefixed_link(
        quotation.quotation_to,
        src.party_name,
        company_abbr
    )
    quotation.party_name = resolved_party_name

    # --------------------------------------------------
    # MAP MAIN FIELDS
    # payment_terms_template is deliberately excluded here - see comment
    # at top of file. Without this exclusion, set_missing_values() below
    # regenerates payment_schedule from the template right after we clear
    # it, which is what produces the duplicate-due-date validation error.
    #
    # customer_address / shipping_address_name / contact_person are also
    # excluded from the blind copy - see SOFT_VALIDATED_HEADER_FIELDS -
    # because they reference records that may not exist, or may not
    # belong to this party, on the target system. They're validated and
    # set separately below; if invalid they're just left empty rather
    # than failing the whole record.
    # --------------------------------------------------
    for field, value in src.as_dict().items():

        if (
            field in SYSTEM_FIELDS
            or field in CHILD_TABLES
            or field in EXPLICITLY_HANDLED_HEADER_FIELDS
            or field in SOFT_VALIDATED_HEADER_FIELDS
        ):
            continue

        if field in quotation.meta.get_valid_columns():
            quotation.set(field, value)

    quotation.payment_terms_template = None

    # --------------------------------------------------
    # ADDRESS / CONTACT FIELDS (soft-validated)
    # Leave empty instead of failing the sync when the source value
    # doesn't exist, or doesn't belong to this party, on this system.
    # --------------------------------------------------
    for field in ADDRESS_FIELDS_TO_VALIDATE:
        if field not in quotation.meta.get_valid_columns():
            continue
        src_value = src.get(field)
        quotation.set(field, src_value if _is_address_valid(src_value) else None)

    for field in CONTACT_FIELDS_TO_VALIDATE:
        if field not in quotation.meta.get_valid_columns():
            continue
        src_value = src.get(field)
        if _is_contact_valid_for_party(src_value, quotation.quotation_to, quotation.party_name):
            quotation.set(field, src_value)
        else:
            quotation.set(field, None)

    # --------------------------------------------------
    # ITEMS
    # --------------------------------------------------
    quotation.set("items", [])

    for row in src.items:
        qty = row.qty or 0
        rate = row.rate or 0

        item_row = {
            "item_code": row.item_code,
            "item_name": row.item_name,
            "description": row.description,
            "qty": qty,
            "uom": row.uom,
            "rate": rate,
            "amount": qty * rate,
            "base_rate": rate,
            "base_amount": qty * rate,
            "conversion_factor": getattr(row, "conversion_factor", 1) or 1,
            "discount_percentage": row.discount_percentage or 0
        }

        for remote_field in ("custom_remote_id", "remote_id"):
            if frappe.get_meta("Quotation Item").has_field(remote_field):
                item_row[remote_field] = getattr(row, remote_field, None) or row.name
                break

        quotation.append("items", item_row)

    # --------------------------------------------------
    # TAXES
    # --------------------------------------------------
    quotation.set("taxes", [])
    for tax in src.taxes:
        quotation.append("taxes", {
            "charge_type": tax.charge_type,
            "account_head": tax.account_head,
            "description": tax.description,
            "rate": tax.rate or 0
        })

    # --------------------------------------------------
    # PAYMENT SCHEDULE - deliberately left empty. With payment_terms_template
    # cleared above, set_missing_values() falls back to at most a single
    # default schedule row (or none) instead of regenerating multiple rows
    # from a template, so it can no longer collide on due date.
    # --------------------------------------------------
    quotation.set("payment_schedule", [])

    # --------------------------------------------------
    # FLAGS
    # --------------------------------------------------
    quotation.flags.ignore_permissions = True
    quotation.flags.ignore_links = True
    quotation.flags.ignore_mandatory = True
    quotation.flags.ignore_pricing_rule = True

    # set_missing_values() reads party_name immediately for Lead/Customer
    # detail lookup, so keep the resolved local link pinned here too.
    quotation.quotation_to = src.quotation_to or "Customer"
    quotation.party_name = resolved_party_name
    quotation.run_method("set_missing_values")

    # Belt-and-suspenders: even with payment_terms_template cleared, wipe
    # payment_schedule again in case set_missing_values (or any hook)
    # populated it from posting_date/credit_days defaults. We only need
    # calculate_taxes_and_totals for the totals - it doesn't depend on
    # payment_schedule being populated.
    quotation.set("payment_schedule", [])

    # set_missing_values() can also re-populate contact_person /
    # customer_address / shipping_address_name from the party's defaults
    # if we left them empty above. That's fine (those defaults are
    # guaranteed valid for this party) - nothing further needed here.

    quotation.run_method("calculate_taxes_and_totals")

    # --------------------------------------------------
    # SAVE
    # --------------------------------------------------
    if existing_name:
        quotation.save()
    else:
        quotation.insert()

        if quotation.name != target_name:
            if not frappe.db.exists("Quotation", target_name):
                old_name = quotation.name

                frappe.db.sql("""
                    UPDATE `tabQuotation`
                    SET name = %s
                    WHERE name = %s
                """, (target_name, old_name))

                for child_table in ["Quotation Item", "Sales Taxes and Charges"]:
                    if frappe.db.exists("DocType", child_table):
                        frappe.db.sql("""
                            UPDATE `tab{0}`
                            SET parent = %s
                            WHERE parent = %s
                        """.format(child_table), (target_name, old_name))

                quotation.name = target_name

            else:
                frappe.log_error(
                    f"Quotation Sync: could not rename {quotation.name} to "
                    f"{target_name} because that name already exists.",
                    "ExternalQuotation Sync - name collision"
                )

    return quotation


# ==========================================================
# DOCSTATUS SYNC
# ==========================================================

def sync_docstatus(doc, target_status):
    current = doc.docstatus

    if target_status == 0:
        return
    elif target_status == 1:
        if current == 0:
            doc.submit()
    elif target_status == 2:
        if current == 0:
            doc.submit()
            doc.cancel()
        elif current == 1:
            doc.cancel()
