frappe.listview_settings['External Purchase Order'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Purchase Order'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Purchase Order'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Purchase Order records to Purchase Order?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_po_sync.sync_purchase_order_docs',
                        args: {
                            source_doctype: 'External Purchase Order',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Purchase Order synced successfully'),
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