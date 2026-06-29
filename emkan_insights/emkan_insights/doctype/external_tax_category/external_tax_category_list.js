frappe.listview_settings['External Tax Category'] = {
    onload(listview) {
        // Page length settings
        listview.page_length = 50;
        listview.load_more = true;

        // Sync button
        listview.page.add_inner_button(__('Sync to Tax Category'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Tax Category'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Tax Category records to Tax Category master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_tax_category_sync.sync_tax_category_docs',
                        args: {
                            source_doctype: 'External Tax Category',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Tax Category synced successfully'),
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
