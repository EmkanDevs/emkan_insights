frappe.listview_settings['External Item Tax Template'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Item Tax Template'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one Item Tax Template'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Item Tax Template records to Item Tax Template?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_item_tax_template_sync.sync_item_tax_templates',
                        args: {
                            source_doctype: 'External Item Tax Template',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Item Tax Template synced successfully'),
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