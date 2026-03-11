frappe.listview_settings['External Asset Category'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Asset Category'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Asset Category'));
                return;
            }

            const names = selected.map((row) => row.name);

            frappe.confirm(
                __('Sync selected External Asset Category records to Asset Category master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_asset_category.external_asset_category.sync_external_records',
                        args: { names },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Asset Category synced successfully'),
                                    indicator: 'green',
                                });
                                listview.refresh();
                            }
                        },
                    });
                }
            );
        });
    },
};
