frappe.listview_settings['External Opportunity'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Opportunity'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Opportunity'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Opportunity records to Opportunity master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_opportunity.external_opportunity.sync_external_opportunities',
                        args: { names },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected Opportunity record(s)...', [names.length]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Opportunity synced successfully'),
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