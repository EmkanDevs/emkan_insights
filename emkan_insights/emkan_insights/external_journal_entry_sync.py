import frappe
import json
from frappe.utils import flt

def is_stock_account(account):
    if not account: return False
    return frappe.db.get_value("Account", account, "account_type") == "Stock"

def balance_journal_entry(je):
    total_debit = sum(flt(d.debit) for d in je.accounts)
    total_credit = sum(flt(d.credit) for d in je.accounts)
    diff = round(total_debit - total_credit, 2)

    if abs(diff) < 0.01:
        return

    adjustment_account = frappe.db.get_value("Company", je.company, "default_expense_account")
    if not adjustment_account:
        frappe.throw(f"Default Expense Account not set for Company {je.company}.")

    # Apply the difference to the adjustment account
    if diff > 0:
        je.append("accounts", {"account": adjustment_account,"credit_in_account_currency": abs(diff), "credit": abs(diff),"debit_in_account_currency": 0, "debit": 0})
    else:
        je.append("accounts", {"account": adjustment_account,"debit_in_account_currency": abs(diff), "debit": abs(diff),"credit_in_account_currency": 0, "credit": 0})

@frappe.whitelist()
def sync_external_journal_entries(names):
    if isinstance(names, str):
        names = json.loads(names)

    result = {"created": [], "skipped": [], "failed": []}

    for name in names:
        try:
            # 1. STOP DUPLICATE RECORDS
            if frappe.db.exists("Journal Entry", name):
                result["skipped"].append({"external": name, "reason": "Already Synced"})
                continue

            ext = frappe.get_doc("External Journal Entry", name)
            if ext.docstatus != 1:
                result["skipped"].append({"external": name, "reason": "Not Submitted"})
                continue

            # 2. CREATE NEW JE
            je = frappe.new_doc("Journal Entry")
            je.name = ext.name 
            je.voucher_type = ext.voucher_type or "Journal Entry"
            je.posting_date = ext.posting_date
            je.company = ext.company
            je.remark = f"Synced from {ext.name}"
            
            # 3. STRICT GROUPING (Force merge by Account only)
            # This is the "Nuclear Option" to stop the 6x repetition
            account_totals = {}

            for row in ext.accounts:
                if not row.account or is_stock_account(row.account):
                    continue

                acc = row.account
                if acc not in account_totals:
                    account_totals[acc] = {
                        "debit": 0.0,
                        "credit": 0.0,
                        "party_type": row.party_type,
                        "party": row.party,
                        "cost_center": row.cost_center or frappe.get_cached_value("Company", je.company, "cost_center")
                    }
                
                account_totals[acc]["debit"] += flt(row.debit_in_account_currency)
                account_totals[acc]["credit"] += flt(row.credit_in_account_currency)

            # 4. BUILD THE CLEAN ROWS
            final_rows = []
            for acc, vals in account_totals.items():
                net_amount = flt(vals["debit"] - vals["credit"], 2)
                
                if net_amount == 0: continue # Skip if they cancel out
                
                row_data = {
                    "account": acc,
                    "party_type": vals["party_type"],
                    "party": vals["party"],
                    "cost_center": vals["cost_center"],
                    # ADD these two fields:
                    "debit_in_account_currency": net_amount if net_amount > 0 else 0,
                    "credit_in_account_currency": abs(net_amount) if net_amount < 0 else 0,
                    # Keep these for company currency (same if single currency):
                    "debit": net_amount if net_amount > 0 else 0,
                    "credit": abs(net_amount) if net_amount < 0 else 0,
                }
                final_rows.append(row_data)

            if not final_rows:
                result["failed"].append({"external": name, "error": "Zero balance rows"})
                continue

            je.set("accounts", final_rows)

            # 5. BALANCE & SUBMIT
            balance_journal_entry(je)
            
            # Final check: remove any accidentally created 0 rows
            je.set("accounts", [r for r in je.accounts if flt(r.debit) > 0 or flt(r.credit) > 0])

            je.total_debit = sum(flt(r.debit) for r in je.accounts)
            je.total_credit = sum(flt(r.credit) for r in je.accounts)

            je.flags.ignore_permissions = True
            je.flags.ignore_link_validation = True
            je.flags.ignore_validate = True
            je.flags.ignore_links = True 
            # je.insert(set_name=True)
            je.insert() 
            je.submit()

            result["created"].append(je.name)
            frappe.db.commit()

        except Exception as e:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"Sync Error: {name}")
            result["failed"].append({"external": name, "error": str(e)})

    return result