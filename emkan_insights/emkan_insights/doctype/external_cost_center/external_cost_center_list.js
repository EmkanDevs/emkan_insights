frappe.listview_settings['External Cost Center'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Cost Center'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Cost Center'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Cost Center records to Cost Center master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_cost_center_sync.sync_cost_center_docs',
                        args: {
                            source_doctype: 'External Cost Center',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Cost Center synced successfully'),
                                    indicator: 'green'
                                });
                                listview.refresh();
                            }
                        }
                    });
                }
            );
        });
    }
};
