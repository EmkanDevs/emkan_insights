frappe.listview_settings['External Payment Request'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Payment Request'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Payment Request'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Payment Request records to Payment Request?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_payment_request_sync.sync_payment_request_docs',
                        args: {
                            source_doctype: 'External Payment Request',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Payment Request synced successfully'),
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