frappe.listview_settings['External Lead'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Lead'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Contact'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.confirm(
                __('Sync {0} selected record(s) to Contact master?', [names.length]),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_lead.external_lead.sync_external_leads',
                        args: {
                            source_doctype: 'External Contact',
                            names: names
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected record(s)...', [names.length]),
                        callback(r) {
                            if (r.exc) {
                                frappe.msgprint(__('Error: ') + r.exc);
                                return;
                            }
                            
                            const results = r.message || [];
                            const synced = results.filter(x => x.status === 'synced').length;
                            const exists = results.filter(x => x.status === 'exists').length;
                            const errors = results.filter(x => x.status === 'error');
                            
                            let msg = __('Synced: {0}, Exists: {1}', [synced, exists]);
                            if (errors.length) {
                                msg += '<br><br>' + __('Errors:') + '<br>';
                                msg += errors.map(e => `<b>${e.name}</b>: ${e.error}`).join('<br>');
                            }
                            
                            frappe.msgprint({
                                title: __('Sync Results'),
                                message: msg,
                                indicator: errors.length ? 'orange' : 'green'
                            });
                            
                            listview.refresh();
                        }
                    });
                }
            );
        });
    }
};