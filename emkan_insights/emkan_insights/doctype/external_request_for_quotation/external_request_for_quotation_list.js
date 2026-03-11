frappe.listview_settings['External Request for Quotation'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Request for Quotation'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Request for Quotation'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Request for Quotation records to Request for Quotation master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_rfq_sync.sync_rfq_docs',
                        args: {
                            source_doctype: 'External Request for Quotation',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Request for Quotation synced successfully'),
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
