frappe.listview_settings['External Project'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Project'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Project'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Project records to Project?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_project_sync.sync_project_docs',
                        args: {
                            source_doctype: 'External Project',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Project synced successfully'),
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