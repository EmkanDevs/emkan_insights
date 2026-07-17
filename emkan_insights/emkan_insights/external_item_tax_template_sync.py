import json
import frappe
from frappe import _


@frappe.whitelist()
def sync_item_tax_templates(names):
    """
    Sync External Item Tax Template -> Item Tax Template
    """

    if isinstance(names, str):
        names = json.loads(names)

    synced = []
    failed = []

    for name in names:
        try:
            external = frappe.get_doc("External Item Tax Template", name)

            # Find existing document
            existing = frappe.db.get_value(
                "Item Tax Template",
                {"title": external.title},
                "name"
            )

            if existing:
                doc = frappe.get_doc("Item Tax Template", existing)
            else:
                doc = frappe.new_doc("Item Tax Template")

            # --------------------------------------------------
            # Header Fields
            # --------------------------------------------------

            doc.title = external.title
            doc.company = external.company
            doc.disabled = cint(external.disabled or 0)

            # --------------------------------------------------
            # Child Table
            # --------------------------------------------------

            doc.set("taxes", [])

            for row in external.get("taxes", []):
                doc.append("taxes", {
                    "tax_type": row.tax_type,
                    "tax_rate": row.tax_rate
                })

            # --------------------------------------------------
            # Ignore all possible validations
            # --------------------------------------------------

            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.flags.ignore_validate = True
            doc.flags.ignore_validate_update_after_submit = True
            doc.flags.ignore_links = True
            doc.flags.ignore_children = False
            doc.flags.ignore_version = True
            doc.flags.ignore_if_duplicate = True

            if doc.is_new():
                doc.insert(
                    ignore_permissions=True,
                    ignore_links=True,
                    ignore_if_duplicate=True,
                    ignore_mandatory=True
                )
            else:
                doc.save(
                    ignore_permissions=True,
                    ignore_version=True,
                    ignore_mandatory=True
                )

            synced.append(doc.name)

        except Exception:
            frappe.db.rollback()

            failed.append({
                "external_doc": name,
                "error": frappe.get_traceback()
            })

    frappe.db.commit()

    return {
        "status": "Completed",
        "synced_count": len(synced),
        "failed_count": len(failed),
        "synced": synced,
        "failed": failed,
    }


def cint(value):
    try:
        return int(value)
    except Exception:
        return 0