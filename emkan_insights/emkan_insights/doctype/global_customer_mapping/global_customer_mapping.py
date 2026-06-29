import frappe
from frappe.model.document import Document

class GlobalCustomerMapping(Document):
    pass


@frappe.whitelist()
def auto_map_all_customers():

    customers = frappe.get_all(
        "External Customer",
        filters={"tax_id": ["is", "set"]},
        fields=["name", "source_site", "tax_id"]
    )

    grouped = {}

    # Group by Tax ID
    for c in customers:
        grouped.setdefault(c.tax_id, []).append(c)

    created = 0

    for tax_id, records in grouped.items():

        if len(records) <= 1:
            continue

        # Skip if already mapped
        if frappe.db.exists("Global Customer Mapping", {"tax_id": tax_id}):
            continue

        # Naming logic
        mapping_name = records[0].name  # or f"TAX-{tax_id}"

        doc = frappe.get_doc({
            "doctype": "Global Customer Mapping",
            "mapping_name": mapping_name,
            "tax_id": tax_id,
            "customers": []
        })

        for r in records:
           doc.append("customers", {
				"customer": r.name,
				"customer_name": frappe.db.get_value(
					"External Customer", r.name, "customer_name"
				) or r.name,
				"source_site": r.source_site
			})

        doc.insert(ignore_permissions=True)
        created += 1

    frappe.db.commit()

    return f"{created} mapping records created"