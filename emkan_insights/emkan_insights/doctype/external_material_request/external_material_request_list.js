frappe.listview_settings['External Material Request'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Material Request'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Material Request'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Material Request records to Material Request?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_material_request_sync.sync_material_request_docs',
                        args: {
                            source_doctype: 'External Material Request',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {

                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Material Request synced successfully'),
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