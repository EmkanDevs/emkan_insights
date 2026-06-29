frappe.listview_settings['External Sales Stage'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Sales Stage'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Sales Stage'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Sales Stage records to Sales Stage master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_sync.sync_external_docs',
                        args: {
                            source_doctype: 'External Sales Stage',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, 'External Sales Stage']),
                        callback(r) {
                            if (!r.exc) {
                                frappe.msgprint(__('Sync completed successfully'));
                                listview.refresh();
                            }
                        }
                    });
                }
            );
        });
    }
};
