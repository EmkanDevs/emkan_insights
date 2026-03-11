frappe.listview_settings['External Payment Entry'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Payment Entry'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Payment Entry'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Payment Entry records to Payment Entry?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_payment_entry_sync.sync_payment_entry_docs',
                        args: {
                            source_doctype: 'External Payment Entry',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Payment Entry synced successfully'),
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