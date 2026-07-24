frappe.listview_settings['External Stock Reconciliation'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Stock Reconciliation'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Stock Reconciliation'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Stock Reconciliation records to Stock Reconciliation master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_stock_reconciliation_sync.sync_stock_reconciliation_docs',
                        args: {
                            source_doctype: 'External Stock Reconciliation',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (r.exc) {
                                frappe.msgprint({
                                    title: __('Sync Error'),
                                    indicator: 'red',
                                    message: __('Server Error: {0}', [r.exc])
                                });
                                return;
                            }

                            if (!r.message) {
                                frappe.msgprint({
                                    title: __('No Response'),
                                    indicator: 'orange',
                                    message: __('No response from server - check server logs')
                                });
                                return;
                            }

                            const results = r.message;
                            const synced = results.filter(row => row.status === 'synced' || row.status === 'docstatus_synced').length;
                            const exists = results.filter(row => row.status === 'exists').length;
                            const failed = results.filter(row => row.status === 'failed').length;

                            let message = [];
                            if (synced) message.push(__('{0} synced', [synced]));
                            if (exists) message.push(__('{0} already exist', [exists]));
                            if (failed) message.push(__('{0} failed', [failed]));

                            frappe.show_alert({
                                message: message.join(', ') || __('Done'),
                                indicator: failed ? 'orange' : 'green'
                            });

                            listview.refresh();
                        }
                    });
                }
            );
        });
    }
};
