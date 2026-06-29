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

                            if (!r.exc && r.message) {
                                const { summary, results } = r.message;
                                const created = (results || []).filter(x => x.action === 'created').length;
                                const updated = (results || []).filter(x => x.action === 'updated').length;
                                const errors  = (results || []).filter(x => x.action === 'error').length;

                                // Only show the inline alert if the msgprint from the server
                                // didn't already pop up (i.e. no errors). When there ARE errors
                                // the server calls frappe.msgprint itself.
                                if (!errors) {
                                    frappe.show_alert({
                                        message: __(summary),
                                        indicator: (created + updated) > 0 ? 'green' : 'blue'
                                    });
                                }

                                listview.refresh();
                            }

                        }
                    });

                }
            );

        });
    }
};