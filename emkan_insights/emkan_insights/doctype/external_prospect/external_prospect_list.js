frappe.listview_settings['External Prospect'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Prospect'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Prospect'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Prospect records to Prospect master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_prospect.external_prospect.sync_external_prospects',
                        args: { names: names },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected Prospect record(s)...', [names.length]),
                        callback(r) {
                            if (r.exc) {
                                frappe.msgprint({
                                    title: __('Sync Failed'),
                                    message: __('An error occurred during sync. Please check Error Log.'),
                                    indicator: 'red'
                                });
                                return;
                            }
                            frappe.show_alert({
                                message: __('Prospect synced successfully'),
                                indicator: 'green'
                            });
                            listview.refresh();
                        }
                    });
                }
            );
        });
    }
};