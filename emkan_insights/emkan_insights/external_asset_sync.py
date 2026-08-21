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

# Chunk size for splitting large bulk syncs into multiple background jobs
CHUNK_SIZE = 300

from emkan_insights.emkan_insights.registry.decorators import filter_kwargs


# ==========================================================
# WHITELIST ENTRY POINT (CHUNKED)
# ==========================================================

@frappe.whitelist()
@filter_kwargs
def sync_external_assets(source_doctype, names):
    """
    Syncs external assets to local Asset doctype.
    Splits large batches into chunks and enqueues one background job per chunk.
    
    Args:
        source_doctype: The external asset doctype (e.g., 'External Asset')
        names: JSON list of external asset names to sync
    
    Returns:
        String describing how many chunks were queued
    """
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    # De-duplicate incoming names while preserving order
    seen = set()
    unique_names = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    # Split into chunks and enqueue one background job per chunk
    chunks = [
        unique_names[i:i + CHUNK_SIZE]
        for i in range(0, len(unique_names), CHUNK_SIZE)
    ]

    queued_chunks = 0
    for idx, chunk in enumerate(chunks):
        job_name = f"asset_sync_chunk_{frappe.generate_hash(length=8)}_{idx}"
        frappe.enqueue(
            "emkan_insights.emkan_insights.external_asset_sync._sync_external_assets_job",
            queue="long",
            job_name=job_name,
            timeout=1000,
            source_doctype=source_doctype,
            names=chunk,
        )
        queued_chunks += 1

    return {
        "status": "queued",
        "total": len(unique_names),
        "queued_chunks": queued_chunks,
        "message": (
            f"Sync Queued: {len(unique_names)} record(s) split into "
            f"{queued_chunks} chunk(s) of up to {CHUNK_SIZE} each"
        ),
    }


# ==========================================================
# BACKGROUND WORKER
# ==========================================================

def _sync_external_assets_job(source_doctype, names):
    """
    Background worker that processes a single chunk of external asset names.
    """
    if isinstance(names, str):
        names = json.loads(names)

    if not isinstance(names, (list, tuple)):
        return []

    results = []
    for name in names:
        try:
            ext_asset = frappe.get_doc(source_doctype, name)
            sync_started_at = ext_asset.modified

            target_name = ext_asset.name
            existing_asset = frappe.db.exists("Asset", target_name)
            asset = frappe.get_doc("Asset", target_name) if existing_asset else frappe.new_doc("Asset")

            if not existing_asset:
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
                        "depreciation_start_date": fb_row.depreciation_start_date if hasattr(fb_row, 'depreciation_start_date') else None,
                        "frequency_of_depreciation": fb_row.frequency_of_depreciation if hasattr(fb_row, 'frequency_of_depreciation') else None,
                        "expected_value_after_useful_life": fb_row.expected_value_after_useful_life if hasattr(fb_row, 'expected_value_after_useful_life') else 0,
                        "salvage_value_percentage": fb_row.salvage_value_percentage if hasattr(fb_row, 'salvage_value_percentage') else 0,
                        # NOTE: carry the source row's docstatus through explicitly.
                        # We can no longer rely on the loop variable `fb_row` once
                        # we leave this loop, so it has to be captured here.
                        "docstatus": fb_row.docstatus if hasattr(fb_row, 'docstatus') else 0,
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

            # --- MIRROR SUBMIT STATE FROM SOURCE ---
            # This is the piece that was missing before: db_insert() writes
            # whatever is currently on the in-memory doc, and docstatus was
            # never being set, so every synced Asset landed as Draft (0)
            # regardless of the source record's real state.
            asset.docstatus = ext_asset.docstatus if hasattr(ext_asset, 'docstatus') else 0

            if existing_asset:
                frappe.db.delete("Asset Finance Book", {
                    "parent": asset.name,
                    "parenttype": "Asset",
                    "parentfield": "finance_books",
                })
                # Bypass Asset controller validation while still refreshing the
                # local mirror with the latest external values.
                asset.db_update()
            else:
                # Use db_insert to bypass Asset controller validation (same approach
                # as the Material Request sync). The controller runs stock/account
                # checks that fail for externally-imported records.
                asset.db_insert()

            # Manually insert finance_books child rows.
            # We do NOT call fb.submit()/fb.cancel() here — Asset Finance Book
            # is a child doctype and is not independently submittable in most
            # Frappe/ERPNext setups, so calling submit()/cancel() on it either
            # raises DocstatusTransitionError or runs full controller side
            # effects (GL/stock entries, permission checks) that these
            # externally-imported rows were never meant to trigger. Instead we
            # just mirror the docstatus we captured above directly, same
            # bypass strategy used for the parent Asset.
            for fb in asset.get("finance_books") or []:
                fb.parent      = asset.name
                fb.parenttype  = "Asset"
                fb.parentfield = "finance_books"
                if not fb.name:
                    fb.name = frappe.generate_hash("", 10)
                # Child rows should generally just inherit the parent's
                # docstatus rather than tracking their own independently.
                fb.docstatus = asset.docstatus
                fb.db_insert()

            results.append({
                "name": asset.name,
                "status": "updated" if existing_asset else "synced",
                "remote_id": target_name,
                "asset_name": asset.asset_name,
                "docstatus": asset.docstatus
            })

            ext_asset.db_set(
                "last_synced_on",
                sync_started_at,
                update_modified=False
            )

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

        # Call the job function directly so bulk still returns actual results
        results = _sync_external_assets_job(source_doctype, names)

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
