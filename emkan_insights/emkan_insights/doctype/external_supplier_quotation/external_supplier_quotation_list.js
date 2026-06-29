frappe.listview_settings['External Supplier Quotation'] = {
    onload(listview) {
        // Page length settings
        listview.page_length = 50;
        listview.load_more = true;

        // Sync button
        listview.page.add_inner_button(__('Sync to Supplier Quotation'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Supplier Quotation'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Supplier Quotation records to Supplier Quotation master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_supplier_quotation_sync.sync_supplier_quotation_docs',
                        args: {
                            source_doctype: 'External Supplier Quotation',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Supplier Quotation synced successfully'),
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
