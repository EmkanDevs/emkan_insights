import frappe
import json

SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "doctype", "__last_sync_on"
}

# Fields that should not be auto-copied and are handled specially
ASSET_SPECIAL_FIELDS = {
    "name", "remote_id", "item_code", "asset_name",
    "finance_books"  # Child table - handled separately
}

@frappe.whitelist()
def sync_external_assets(source_doctype, names):
    """
    Syncs external assets to local Asset doctype.
    
    Args:
        source_doctype: The external asset doctype (e.g., 'External Asset')
        names: JSON list of external asset names to sync
    
    Returns:
        List of sync results with status information
    """
    if isinstance(names, str):
        names = json.loads(names)

    results = []
    for name in names:
        try:
            ext_asset = frappe.get_doc(source_doctype, name)

            # Check if an Asset with this name already exists locally
            # (We avoid filtering by remote_id because that column may not yet
            #  be present in tabAsset if migrate hasn't been run.)
            target_name = ext_asset.name
            if frappe.db.exists("Asset", target_name):
                results.append({"name": target_name, "status": "exists", "remote_id": target_name})
                continue

            asset = frappe.new_doc("Asset")

            # Force the document name — tells Frappe to skip autoname()
            asset.name = target_name
            asset.set("__newname", target_name)

            # Store remote_id only if the column exists in the DB
            if frappe.db.has_column("Asset", "remote_id"):
                asset.remote_id = target_name

            # --- CORE ASSET FIELDS ---
            # Map essential asset fields
            asset.item_code = ext_asset.item_code
            asset.asset_name = ext_asset.asset_name
            asset.company = ext_asset.company
            asset.asset_category = ext_asset.asset_category
            asset.location = ext_asset.location if hasattr(ext_asset, 'location') else None

            # --- OWNERSHIP & CUSTODIAN ---
            asset.asset_owner = ext_asset.asset_owner if hasattr(ext_asset, 'asset_owner') else None
            asset.custodian = ext_asset.custodian if hasattr(ext_asset, 'custodian') else None
            asset.department = ext_asset.department if hasattr(ext_asset, 'department') else None
            asset.cost_center = ext_asset.cost_center if hasattr(ext_asset, 'cost_center') else None

            # --- DATES ---
            asset.purchase_date = ext_asset.purchase_date
            asset.available_for_use_date = ext_asset.available_for_use_date if hasattr(ext_asset, 'available_for_use_date') else ext_asset.purchase_date
            
            if hasattr(ext_asset, 'disposal_date') and ext_asset.disposal_date:
                asset.disposal_date = ext_asset.disposal_date

            # --- ASSET QUANTITY & COST ---
            asset.asset_quantity = ext_asset.asset_quantity if hasattr(ext_asset, 'asset_quantity') else 1
            asset.gross_purchase_amount = ext_asset.net_purchase_amount if hasattr(ext_asset, 'net_purchase_amount') else 0
            asset.purchase_receipt = ext_asset.purchase_receipt if hasattr(ext_asset, 'purchase_receipt') else None
            asset.purchase_invoice = ext_asset.purchase_invoice if hasattr(ext_asset, 'purchase_invoice') else None

            # --- DEPRECIATION SETTINGS ---
            asset.calculate_depreciation = ext_asset.calculate_depreciation if hasattr(ext_asset, 'calculate_depreciation') else 1
            
            if hasattr(ext_asset, 'depreciation_method') and ext_asset.depreciation_method:
                asset.depreciation_method = ext_asset.depreciation_method
            
            if hasattr(ext_asset, 'total_number_of_depreciations') and ext_asset.total_number_of_depreciations:
                asset.total_number_of_depreciations = ext_asset.total_number_of_depreciations
            
            if hasattr(ext_asset, 'frequency_of_depreciation_months') and ext_asset.frequency_of_depreciation_months:
                asset.frequency_of_depreciation_months = ext_asset.frequency_of_depreciation_months
            
            if hasattr(ext_asset, 'next_depreciation_date') and ext_asset.next_depreciation_date:
                asset.next_depreciation_date = ext_asset.next_depreciation_date
            
            # Opening depreciation data (for existing assets)
            if hasattr(ext_asset, 'opening_accumulated_depreciation'):
                asset.opening_accumulated_depreciation = ext_asset.opening_accumulated_depreciation or 0.0
            
            if hasattr(ext_asset, 'opening_number_of_booked_depreciations'):
                asset.opening_number_of_booked_depreciations = ext_asset.opening_number_of_booked_depreciations or 0

            # --- STATUS & FLAGS ---
            asset.status = ext_asset.status if hasattr(ext_asset, 'status') else "Draft"
            asset.is_existing_asset = ext_asset.is_existing_asset if hasattr(ext_asset, 'is_existing_asset') else 0
            asset.is_composite_asset = ext_asset.is_composite_asset if hasattr(ext_asset, 'is_composite_asset') else 0
            asset.is_fully_depreciated = ext_asset.is_fully_depreciated if hasattr(ext_asset, 'is_fully_depreciated') else 0

            # --- INSURANCE DETAILS ---
            if hasattr(ext_asset, 'policy_number') and ext_asset.policy_number:
                asset.policy_number = ext_asset.policy_number
            
            if hasattr(ext_asset, 'insurer') and ext_asset.insurer:
                asset.insurer = ext_asset.insurer
            
            if hasattr(ext_asset, 'insured_value') and ext_asset.insured_value:
                asset.insured_value = ext_asset.insured_value
            
            if hasattr(ext_asset, 'insurance_start_date') and ext_asset.insurance_start_date:
                asset.insurance_start_date = ext_asset.insurance_start_date
            
            if hasattr(ext_asset, 'insurance_end_date') and ext_asset.insurance_end_date:
                asset.insurance_end_date = ext_asset.insurance_end_date

            # --- MAINTENANCE ---
            if hasattr(ext_asset, 'maintenance_required'):
                asset.maintenance_required = ext_asset.maintenance_required

            # --- FINANCE BOOKS (Child Table) ---
            # Only sync if the source has finance_books and Asset doctype supports it
            if hasattr(ext_asset, 'finance_books') and ext_asset.finance_books:
                asset.set("finance_books", [])
                for fb_row in ext_asset.finance_books:
                    fb_dict = {
                        "finance_book": fb_row.finance_book if hasattr(fb_row, 'finance_book') else None,
                        "depreciation_method": fb_row.depreciation_method if hasattr(fb_row, 'depreciation_method') else None,
                        "rate_of_depreciation": fb_row.rate_of_depreciation if hasattr(fb_row, 'rate_of_depreciation') else 0,
                        "frequency_of_depreciation_months": fb_row.frequency_of_depreciation_months if hasattr(fb_row, 'frequency_of_depreciation_months') else None,
                        "total_number_of_booked_depreciations": fb_row.total_number_of_booked_depreciations if hasattr(fb_row, 'total_number_of_booked_depreciations') else 0,
                        "total_number_of_depreciations": fb_row.total_number_of_depreciations if hasattr(fb_row, 'total_number_of_depreciations') else 0,
                        "expected_value_after_useful_life": fb_row.expected_value_after_useful_life if hasattr(fb_row, 'expected_value_after_useful_life') else 0,
                        "salvage_value_percentage": fb_row.salvage_value_percentage if hasattr(fb_row, 'salvage_value_percentage') else 0,
                    }
                    # Only add if finance_book is specified
                    if fb_dict.get("finance_book"):
                        asset.append("finance_books", fb_dict)

            # --- COPY REMAINING CUSTOM FIELDS ---
            # This will handle any additional custom fields defined on the Asset doctype
            for field, value in ext_asset.as_dict().items():
                if (field not in SYSTEM_FIELDS and 
                    field not in ASSET_SPECIAL_FIELDS and 
                    hasattr(asset, field) and
                    value is not None):
                    try:
                        asset.set(field, value)
                    except Exception as e:
                        frappe.log_error(
                            f"Could not copy field {field} for asset {name}: {str(e)}",
                            "Asset Sync - Field Copy Error"
                        )

            # Use db_insert to bypass Asset controller validation (same approach
            # as the Material Request sync). The controller runs stock/account
            # checks that fail for externally-imported records.
            asset.flags.ignore_links     = True
            asset.flags.ignore_mandatory = True
            asset.db_insert()

            # Manually insert finance_books child rows
            for fb in asset.get("finance_books") or []:
                fb.parent      = asset.name
                fb.parenttype  = "Asset"
                fb.parentfield = "finance_books"
                if not fb.name:
                    fb.name = frappe.generate_hash("", 10)
                fb.flags.ignore_links     = True
                fb.flags.ignore_mandatory = True
                fb.db_insert()

            frappe.db.commit()
            results.append({
                "name": asset.name,
                "status": "synced",
                "remote_id": target_name,
                "asset_name": asset.asset_name
            })

        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Asset Sync Error: {name}")
            results.append({
                "name": name,
                "status": "failed",
                "error": "See error log for details"
            })
            continue

    return results


@frappe.whitelist()
def sync_external_asset_bulk(source_doctype):
    """
    Bulk sync all external assets from a given doctype.
    
    Args:
        source_doctype: The external asset doctype (e.g., 'External Asset')
    
    Returns:
        Summary of bulk sync results
    """
    try:
        # Get all external assets that haven't been synced yet
        external_assets = frappe.get_list(
            source_doctype,
            filters={"sync_status": ["!=", "synced"]},  # Adjust filter based on your fields
            fields=["name"],
            limit_page_length=500
        )
        
        names = [asset["name"] for asset in external_assets]
        
        if not names:
            return {
                "status": "success",
                "message": "No new assets to sync",
                "total": 0,
                "synced": 0
            }
        
        results = sync_external_assets(source_doctype, names)
        
        synced_count = len([r for r in results if r["status"] == "synced"])
        existing_count = len([r for r in results if r["status"] == "exists"])
        failed_count = len([r for r in results if r["status"] == "failed"])
        
        return {
            "status": "success",
            "total": len(results),
            "synced": synced_count,
            "existing": existing_count,
            "failed": failed_count,
            "details": results
        }
        
    except Exception:
        frappe.log_error("Bulk Asset Sync Error", frappe.get_traceback())
        return {
            "status": "error",
            "message": "Bulk sync failed. Check error log for details."
        }