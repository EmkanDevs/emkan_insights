frappe.listview_settings['External Asset'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Asset'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Asset'));
                return;
            }

            const names = selected.map((row) => row.name);

            frappe.confirm(
                __('Sync selected External Asset records to Asset master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_asset.external_asset.sync_external_records',
                        args: { names },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Asset synced successfully'),
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
