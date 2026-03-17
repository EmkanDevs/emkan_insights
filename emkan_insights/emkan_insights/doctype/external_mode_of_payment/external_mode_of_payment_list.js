frappe.listview_settings['External Mode of Payment'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Mode of Payment'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Mode of Payment'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Mode of Payment records to Mode of Payment master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_sync.sync_external_docs',
                        args: {
                            source_doctype: 'External Mode of Payment',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, 'External Mode of Payment']),
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
```

Save this as:
```
emkan_insights/emkan_insights/doctype/external_mode_of_payment/external_mode_of_payment_list.js
```

And the corresponding Python method path it calls would be:
```
emkan_insights/emkan_insights/doctype/external_mode_of_payment/external_mode_of_payment.py