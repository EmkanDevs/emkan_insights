frappe.listview_settings['External Asset'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Asset'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Asset'));
                return;
            }

            const names = selected.map((row) => row.name);

            frappe.confirm(
                __('Sync selected External Asset records to Asset master?'),
                () => {
                    frappe.call({
                        method: 'emkan_insights.emkan_insights.doctype.external_asset.external_asset.sync_external_records',
                        args: { names },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, listview.doctype]),
                        callback(r) {
                            if (r.exc) return;

                            const results = r.message || [];
                            const synced   = results.filter(x => x.status === 'synced').length;
                            const exists   = results.filter(x => x.status === 'exists').length;
                            const failed   = results.filter(x => x.status === 'failed').length;

                            const parts = [];
                            if (synced)  parts.push(__('Created: {0}', [synced]));
                            if (exists)  parts.push(__('Already Existed: {0}', [exists]));
                            if (failed)  parts.push(__('Failed: {0}', [failed]));

                            const summary = parts.length ? parts.join(', ') : __('No assets processed');
                            const indicator = failed > 0 ? 'orange' : (synced > 0 ? 'green' : 'blue');

                            frappe.show_alert({ message: summary, indicator });

                            if (failed > 0) {
                                const failedNames = results
                                    .filter(x => x.status === 'failed')
                                    .map(x => `<li>${x.name}: ${x.error || 'See error log'}</li>`)
                                    .join('');
                                frappe.msgprint({
                                    title: __('Sync Errors'),
                                    indicator: 'red',
                                    message: `<ul>${failedNames}</ul>`,
                                });
                            }

                            listview.refresh();
                        },
                    });
                }
            );
        });
    },
};
