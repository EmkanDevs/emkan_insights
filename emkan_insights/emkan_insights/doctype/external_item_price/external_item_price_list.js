frappe.listview_settings['External Item Price'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Item Price'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Item Price'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Item Price records to Item Price master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_item_price.sync_external_item_prices',
                        args: {
                            source_doctype: 'External Item Price',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [
                            names.length,
                            listview.doctype
                        ]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Item Price synced successfully'),
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