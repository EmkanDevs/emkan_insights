frappe.listview_settings['External Customer'] = {
    onload(listview) {

        // ✅ Sync Button (same)
        listview.page.add_inner_button(__('Sync to Customer'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Customer'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync selected External Customer records to Customer master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.external_sync.sync_external_docs',
                        args: {
                            source_doctype: 'External Customer',
                            names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Customer synced successfully'),
                                    indicator: 'green'
                                });
                                listview.refresh();
                            }
                        }
                    });
                }
            );
        });


        // 🚀 AUTO MAP BUTTON (FIXED)
        listview.page.add_inner_button(__('Auto Map by Tax ID'), () => {

            frappe.confirm(
                __('This will map ALL customers by Tax ID. Continue?'),
                () => {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.global_entity_mapping.global_entity_mapping.auto_map_entities',
                        args: {
                            entity_type: 'External Customer',
                            key_field: 'tax_id'
                        },
                        freeze: true,
                        freeze_message: __('Mapping customers...'),
                        callback(r) {
                            if (!r.exc) {
                                frappe.msgprint(r.message || "Mapping completed");
                                listview.refresh();
                            }
                        }
                    });

                }
            );

        });

    }
};