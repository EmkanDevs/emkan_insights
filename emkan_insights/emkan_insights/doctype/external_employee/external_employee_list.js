frappe.listview_settings['External Employee'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Employee'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Employee'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Employee records to Employee?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_employee.external_employee.sync_employee_docs',
                        args: {
                            source_doctype: 'External Employee',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Employee synced successfully'),
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