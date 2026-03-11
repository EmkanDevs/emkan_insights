frappe.listview_settings['External Supplier'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Supplier'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Supplier'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Supplier records to Supplier master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_supplier.external_supplier.sync_external_suppliers',
                        args: {
                            external_suppliers: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Supplier synced successfully'),
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
