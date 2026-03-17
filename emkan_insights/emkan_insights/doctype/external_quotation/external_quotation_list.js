frappe.listview_settings['External Quotation'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Quotation'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Quotation'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Quotation records to Quotation master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_quotation_sync.sync_external_quotation_docs',
                        args: {
                            source_doctype: 'External Quotation',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Quotation synced successfully'),
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
