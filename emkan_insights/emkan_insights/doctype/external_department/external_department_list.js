frappe.listview_settings['External Department'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Department'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Department'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Department records to Department?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_department.external_department.sync_department_docs',
                        args: {
                            source_doctype: 'External Department',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Department synced successfully'),
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