frappe.listview_settings['External Prospect'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Prospect'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Prospect'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Prospect records to Prospect master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_sync.sync_external_docs',
                        args: {
                            source_doctype: 'External Prospect',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Prospect synced successfully'),
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