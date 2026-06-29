# Copyright (c) 2026, Mukesh Variyani and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GlobalEntityMapping(Document):
    pass


@frappe.whitelist()
def auto_map_entities(entity_type=None, key_field=None):
    if not entity_type or not key_field:
        frappe.throw("entity_type and key_field are required")

    # 1. Only find keys where at least one record is NOT yet mapped
    # This prevents re-processing groups that are already fully handled
    shared_ids_query = f"""
        SELECT `{key_field}`
        FROM `tab{entity_type}`
        WHERE `{key_field}` IS NOT NULL 
          AND `{key_field}` != '' 
          AND is_mapped = 0
        GROUP BY `{key_field}`
        HAVING COUNT(*) > 1
    """
    
    shared_ids = frappe.db.sql_list(shared_ids_query)

    if not shared_ids:
        return f"No new {entity_type}s to map."

    # 2. Get or Create the Mapping Parent
    existing_name = frappe.db.get_value("Global Entity Mapping", 
        {"entity_type": entity_type, "key_field": key_field})

    doc = frappe.get_doc("Global Entity Mapping", existing_name) if existing_name else frappe.new_doc("Global Entity Mapping")
    if not existing_name:
        doc.entity_type = entity_type
        doc.key_field = key_field

    # 3. Fetch only the unmapped records for these shared IDs
    records = frappe.get_all(
        entity_type,
        filters={
            key_field: ["in", shared_ids],
            "is_mapped": 0
        },
        fields=["name", key_field, "source_site"],
        order_by=f"`{key_field}` asc"
    )

    # 4. Populate and Save
    for r in records:
        doc.append("mapping", {
            "entity_type": entity_type,
            "entity_name": r.name,
            "key_value": r.get(key_field),
            "source_site": r.get("source_site")
            
        })


    doc.save(ignore_permissions=True)

    # 5. Bulk Update the source records to prevent duplicate processing next time
    # This is much faster than calling .save() on every record
    record_names = [r.name for r in records]
    frappe.db.set_value(entity_type, {"name": ["in", record_names]}, "is_mapped", 1, update_modified=False)

    return f"Successfully added {len(records)} new mappings."