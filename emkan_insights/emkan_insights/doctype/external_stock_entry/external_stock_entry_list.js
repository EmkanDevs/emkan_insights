frappe.listview_settings['External Stock Entry'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Stock Entry'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Stock Entry'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Stock Entry records to Stock Entry master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_stock_entry_sync.sync_stock_entry_docs',
                        args: {
                            source_doctype: 'External Stock Entry',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Stock Entry synced successfully'),
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
