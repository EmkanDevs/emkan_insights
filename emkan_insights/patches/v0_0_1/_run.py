import frappe


def go():
    from emkan_insights.patches.v0_0_1.normalize_source_site_values import execute
    try:
        execute()
    except Exception:
        print(frappe.get_traceback())
        raise
