import frappe
import json


@frappe.whitelist()
def sync_purchase_invoice_docs(source_doctype: str, names):

    if isinstance(names, str):
        names = json.loads(names)

    created_docs = []

    for name in names:
        external = frappe.get_doc("External Purchase Invoice", name)

        if not external.company:
            frappe.throw(f"Company missing in External Purchase Invoice: {name}")

        docname = sync_external_pi(name, external.company)
        created_docs.append(docname)

    return created_docs


def get_po_detail(purchase_order, item_code, qty):
    """
    Fetch the Purchase Order Item row name (po_detail) by matching
    item_code and qty from the given Purchase Order.
    """
    if not purchase_order:
        return None

    # Try exact match on item_code + qty first
    po_detail = frappe.db.get_value(
        "Purchase Order Item",
        {
            "parent": purchase_order,
            "item_code": item_code,
            "qty": qty,
        },
        "name",
    )

    # Fallback: match on item_code only (first row found)
    if not po_detail:
        po_detail = frappe.db.get_value(
            "Purchase Order Item",
            {
                "parent": purchase_order,
                "item_code": item_code,
            },
            "name",
        )

    return po_detail


def get_pr_detail(purchase_receipt, item_code, qty):
    """
    Fetch the Purchase Receipt Item row name (pr_detail) by matching
    item_code and qty from the given Purchase Receipt.
    """
    if not purchase_receipt:
        return None

    # Try exact match on item_code + qty first
    pr_detail = frappe.db.get_value(
        "Purchase Receipt Item",
        {
            "parent": purchase_receipt,
            "item_code": item_code,
            "qty": qty,
        },
        "name",
    )

    # Fallback: match on item_code only (first row found)
    if not pr_detail:
        pr_detail = frappe.db.get_value(
            "Purchase Receipt Item",
            {
                "parent": purchase_receipt,
                "item_code": item_code,
            },
            "name",
        )

    return pr_detail


def sync_external_pi(external_name, company):

    external = frappe.get_doc("External Purchase Invoice", external_name)

    target_name = external.remote_id or external.name

    if frappe.db.exists("Purchase Invoice", target_name):
        return target_name

    pi = frappe.new_doc("Purchase Invoice")
    pi.name = target_name

    # -------------------------
    # Parent Fields
    # -------------------------

    pi.company = company
    pi.supplier = external.supplier
    pi.supplier_name = external.supplier_name

    pi.posting_date = external.posting_date
    pi.posting_time = external.posting_time

    pi.due_date = external.due_date

    pi.bill_no = (
        getattr(external, "bill_no", None)
        or getattr(external, "supplier_invoice_no", None)
    )
    pi.bill_date = (
        getattr(external, "bill_date", None)
        or getattr(external, "supplier_invoice_date", None)
    )

    pi.currency = external.currency
    pi.conversion_rate = external.conversion_rate or 1

    pi.credit_to = external.credit_to

    pi.project = external.project
    pi.cost_center = external.cost_center

    pi.update_stock = external.update_stock or 0

    pi.is_return = external.is_return or 0
    pi.return_against = external.return_against

    pi.remarks = external.remarks

    # -------------------------
    # Items
    # -------------------------

    for row in external.items:

        item = pi.append("items", {})

        item.item_code = row.item_code
        item.item_name = row.item_name
        item.description = row.description

        item.qty = row.qty or 0

        item.uom = row.uom
        item.stock_uom = row.stock_uom

        item.conversion_factor = row.conversion_factor or 1

        item.rate = row.rate or 0
        item.amount = row.amount or 0

        item.base_rate = row.base_rate or row.rate or 0
        item.base_amount = row.base_amount or row.amount or 0

        item.net_rate = row.net_rate or row.rate or 0
        item.net_amount = row.net_amount or row.amount or 0

        item.warehouse = row.warehouse

        item.expense_account = row.expense_account
        item.cost_center = row.cost_center

        item.project = row.project

        # -------------------------
        # PO reference: look up the actual PO Item row name
        # -------------------------
        item.purchase_order = row.purchase_order
        item.po_detail = (
            # use whatever was stored on external row if present
            getattr(row, "purchase_order_item", None)
            or getattr(row, "po_detail", None)
            # otherwise look it up from the PO by item + qty
            or get_po_detail(row.purchase_order, row.item_code, row.qty)
        )

        # -------------------------
        # PR reference: look up the actual PR Item row name
        # -------------------------
        item.purchase_receipt = row.purchase_receipt
        item.pr_detail = (
            getattr(row, "purchase_receipt_item", None)
            or getattr(row, "pr_detail", None)
            or get_pr_detail(row.purchase_receipt, row.item_code, row.qty)
        )

        item.batch_no = row.batch_no
        item.serial_no = row.serial_no

    # -------------------------
    # Taxes
    # -------------------------

    if hasattr(external, "taxes"):
        for tax in external.taxes:
            t = pi.append("taxes", {})
            t.charge_type = tax.charge_type
            t.account_head = tax.account_head
            t.description = tax.description
            t.rate = tax.rate
            t.tax_amount = tax.tax_amount
            t.base_tax_amount = tax.base_tax_amount
            t.cost_center = tax.cost_center

    # -------------------------
    # Payment Schedule
    # -------------------------

    if hasattr(external, "payment_schedule"):
        for pay in external.payment_schedule:
            ps = pi.append("payment_schedule", {})
            ps.payment_term = pay.payment_term
            ps.due_date = pay.due_date
            ps.invoice_portion = pay.invoice_portion
            ps.payment_amount = pay.payment_amount
            ps.mode_of_payment = pay.mode_of_payment

    # -------------------------
    # Flags
    # -------------------------

    pi.flags.ignore_permissions = True
    pi.flags.ignore_validate = True
    pi.flags.ignore_mandatory = True

    pi.billing_address = None
    pi.shipping_address = None

    pi.insert(ignore_permissions=True)

    if external.docstatus == 1:
        pi.flags.ignore_permissions = True
        pi.submit()

    return pi.name