frappe.listview_settings['External Item Group'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Item Group'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Item Group'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Item Group records to Item Group master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_sync.sync_external_docs',
                        args: {
                            source_doctype: 'External Item Group',
                            names
                        },
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Sync queued — {0} Item Group(s) will be processed in the background.', [names.length]),
                                    indicator: 'blue'
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
