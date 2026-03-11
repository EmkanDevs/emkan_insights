frappe.listview_settings['External Purchase Receipt'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Purchase Receipt'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Purchase Receipt'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Purchase Receipt records to Purchase Receipt master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_pr_sync.sync_purchase_receipt_docs',
                        args: {
                            source_doctype: 'External Purchase Receipt',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Purchase Receipt synced successfully'),
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
