/**
 * Global Sync Button for "External " Doctypes
 * Automatically adds a "Sync Data" button to the list view.
 * Respects existing "Sync" buttons to avoid duplication.
 */

window.setup_global_external_sync = function (listview) {
    const doctype = listview.doctype;

    // Only for "External " doctypes, excluding configuration
    if (!doctype || !doctype.startsWith('External ') ||
        doctype === 'External Site Configuration' ||
        doctype === 'External Site Configuration CT') {
        return;
    }

    const label = __('Sync Data');

    // Robust check for existing sync buttons (manual or global)
    // We check for labels containing "Sync" to be safe
    const existing_buttons = listview.page.inner_toolbar ? listview.page.inner_toolbar.find('button') : [];
    let already_has_sync = false;

    if (existing_buttons.length) {
        existing_buttons.each(function () {
            const btn_text = $(this).text().trim();
            if (btn_text.includes(__('Sync'))) {
                already_has_sync = true;
                return false;
            }
        });
    }

    if (already_has_sync) {
        console.log(`[Sync] ${doctype} already has a sync button. Skipping global injection.`);
        return;
    }

    console.log(`[Sync] Adding global Sync Data button to ${doctype}`);

    listview.page.add_inner_button(label, () => {
        const selected = listview.get_checked_items();
        if (!selected.length) {
            frappe.msgprint(__("Please select at least one record"));
            return;
        }
        const names = selected.map(d => d.name);

        frappe.confirm(__('Sync selected records to target?'), () => {
            frappe.call({
                method: "emkan_insights.emkan_insights.api.sync_external_docs",
                args: {
                    source_doctype: doctype,
                    names: names
                },
                callback(r) {
                    if (!r.exc) {
                        frappe.show_alert({
                            message: __("Records synced successfully"),
                            indicator: "green"
                        });
                        listview.refresh();
                    }
                }
            });
        });
    });
};

// 🌍 Global automatic setup for all "External " doctypes in LIST VIEW
$(document).on('listview_setup', function (e, listview) {
    // Small delay to ensure manual listview_settings have loaded and buttons added
    setTimeout(() => {
        window.setup_global_external_sync(listview);
    }, 500);
});
