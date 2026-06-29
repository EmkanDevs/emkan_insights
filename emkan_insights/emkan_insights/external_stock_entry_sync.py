import frappe
import json
from frappe.utils import cint

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on",
    "parent", "parentfield", "parenttype"
}


@frappe.whitelist()
def sync_stock_entry_docs(source_doctype=None, names=None):
    """
    Handles bulk sync from List View.
    """
    # Use print for guaranteed console output
    print("\n" + "="*60)
    print("SYNC STOCK ENTRY DOCS CALLED")
    print(f"source_doctype={source_doctype}")
    print(f"names type={type(names)}")
    print(f"names={names}")
    print("="*60)

    if isinstance(names, str):
        names = json.loads(names)

    print(f"Parsed names count={len(names) if names else 0}")

    if not names:
        print("NO NAMES PROVIDED - returning empty")
        return []

    results = []

    for name in names:
        try:
            print(f"\n--- Processing name={name} ---")
            external_doc = frappe.get_doc("External Stock Entry", name)
            print(f"Fetched external_doc: {external_doc.name}, company={external_doc.company}")
            result = sync_external_stock_entry(name, external_doc.company)
            print(f"Result: {result}")
            results.append(result)
        except Exception as e:
            error_msg = frappe.get_traceback()
            # CORRECT: title first, message second
            frappe.log_error(
                title=f"Stock Entry Sync Error: {name}",
                message=error_msg
            )
            print(f"ERROR for {name}: {str(e)}")
            results.append({
                "name": name,
                "status": "failed",
                "error": str(e)
            })

    print(f"\nFinal results count: {len(results)}")
    print(f"Results: {results}")
    return results


def sync_external_stock_entry(external_name, company):
    """
    Sync a single External Stock Entry to Stock Entry.
    """
    print(f"\n>>> sync_external_stock_entry called for {external_name}")

    external = frappe.get_doc("External Stock Entry", external_name)
    target_name = external.remote_id or external.name

    print(f"target_name={target_name}")
    print(f"external.remote_id={external.remote_id}")
    print(f"external.name={external.name}")

    # 1. Check if already exists
    exists = frappe.db.exists("Stock Entry", target_name)
    print(f"frappe.db.exists('Stock Entry', '{target_name}') = {exists}")

    if exists:
        print(f"Already exists! Returning 'exists'")
        frappe.db.set_value("External Stock Entry", external_name, "remote_id", target_name)
        return {
            "name": target_name,
            "status": "exists"
        }

    # 2. Create new Stock Entry
    print("Creating new Stock Entry...")
    se = frappe.new_doc("Stock Entry")
    se.remote_id = external.remote_id or external.name

    # Copy main fields
    for field, value in external.as_dict().items():
        if (
            field not in SYSTEM_FIELDS
            and field not in ["items", "remote_id", "additional_costs"]
            and hasattr(se, field)
        ):
            se.set(field, value)

    se.company = company
    print(f"Company set to: {company}")

    # Items
    if hasattr(external, "items"):
        item_count = len(external.items) if external.items else 0
        print(f"External has {item_count} items")
        se.set("items", [])

        for idx, row in enumerate(external.items):
            item_row = {}
            for field, value in row.as_dict().items():
                if field not in SYSTEM_FIELDS:
                    item_row[field] = value

            # Map External Item to local Item
            if item_row.get("item_code"):
                mapped_item = frappe.db.get_value("External Item", item_row["item_code"], "remote_id")
                print(f"  Item {idx}: {item_row['item_code']} -> mapped={mapped_item}")
                if mapped_item:
                    item_row["item_code"] = mapped_item

            se.append("items", item_row)

    else:
        print("WARNING: External has NO items attribute!")

    # Flags
    se.flags.ignore_permissions = True
    se.flags.ignore_validate = True
    se.flags.ignore_mandatory = True
    se.flags.ignore_links = True

    print(f"About to insert. se.name before insert = {se.name}")

    # INSERT
    try:
        se.insert(
            ignore_permissions=True,
            ignore_links=True,
            ignore_mandatory=True
        )
        print(f"Insert SUCCESS! se.name after insert = {se.name}")
    except Exception as e:
        print(f"Insert FAILED: {str(e)}")
        frappe.log_error(
            title=f"Stock Entry Insert FAILED: {external_name}",
            message=frappe.get_traceback()
        )
        raise

    # Rename if needed
    if se.name != target_name:
        print(f"Renaming {se.name} -> {target_name}")
        frappe.db.sql("""
            UPDATE `tabStock Entry`
            SET name = %s
            WHERE name = %s
        """, (target_name, se.name))

        # Rename child table
        frappe.db.sql("""
            UPDATE `tabStock Entry Detail`
            SET parent = %s
            WHERE parent = %s
        """, (target_name, se.name))

        se.name = target_name
    else:
        print(f"No rename needed")

    # Map back
    frappe.db.set_value("External Stock Entry", external_name, "remote_id", target_name)
    if frappe.get_meta("Stock Entry").has_field("remote_id"):
        frappe.db.set_value("Stock Entry", target_name, "remote_id", external.remote_id)

    print(f"Set remote_id mapping complete")

    # Submit if needed
    if cint(external.docstatus) == 1:
        print(f"Submitting (docstatus=1)")
        se = frappe.get_doc("Stock Entry", target_name)
        se.submit()

    print(f"<<< DONE, returning {target_name}")
    return {
        "name": target_name,
        "status": "synced"
    }