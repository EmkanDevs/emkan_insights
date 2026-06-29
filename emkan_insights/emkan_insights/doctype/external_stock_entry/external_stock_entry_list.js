frappe.listview_settings['External Stock Entry'] = {
    onload(listview) {
        console.log("External Stock Entry listview loaded");

        listview.page.add_inner_button(__('Sync to Stock Entry'), () => {
            console.log("Sync button clicked");

            const selected = listview.get_checked_items();
            console.log("Selected items:", selected);

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Stock Entry'));
                return;
            }

            const names = selected.map(row => row.name);
            console.log("Names to sync:", names);

            frappe.confirm(
                __('Sync selected External Stock Entry records to Stock Entry master?'),
                () => {
                    console.log("Confirmation accepted, calling method...");
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_stock_entry_sync.sync_stock_entry_docs',
                        args: {
                            source_doctype: 'External Stock Entry',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            console.log("Callback received:", r);
                            if (r.exc) {
                                console.error("Exception in response:", r.exc);
                                frappe.msgprint({
                                    title: __('Sync Error'),
                                    indicator: 'red',
                                    message: __('Server Error: {0}', [r.exc])
                                });
                                return;
                            }

                            if (!r.message) {
                                console.error("No message in response");
                                frappe.msgprint({
                                    title: __('No Response'),
                                    indicator: 'orange',
                                    message: __('No response from server - check server logs')
                                });
                                return;
                            }

                            const results = r.message;
                            console.log("Results:", results);

                            const synced = results.filter(r => r.status === 'synced').length;
                            const exists = results.filter(r => r.status === 'exists').length;
                            const failed = results.filter(r => r.status === 'failed').length;

                            let message = [];
                            if (synced) message.push(__('{0} synced', [synced]));
                            if (exists) message.push(__('{0} already exist', [exists]));
                            if (failed) message.push(__('{0} failed', [failed]));

                            frappe.show_alert({
                                message: message.join(', '),
                                indicator: failed ? 'orange' : 'green'
                            });

                            listview.refresh();
                        },
                        error: (xhr, textStatus, errorThrown) => {
                            console.error("AJAX error:", xhr, textStatus, errorThrown);
                            frappe.msgprint({
                                title: __('Network/Server Error'),
                                indicator: 'red',
                                message: __('Failed to call server method. Check browser console and server logs.<br>Status: {0}<br>Error: {1}', [textStatus, errorThrown])
                            });
                        }
                    });
                }
            );
        });
    }
};