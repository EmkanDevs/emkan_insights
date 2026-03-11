frappe.listview_settings['External Email Template'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Email Template'), () => {
            // const selected = listview.get_checked_items();

            // if (!selected.length) {
            //     frappe.msgprint(__('Please select at least one External Email Template'));
            //     return;
            // }

            // const names = selected.map(row => row.name);

            // frappe.confirm(
            //     __('Sync selected External Email Template records to Email Template master?'),
            //     () => {
            //         frappe.call({
            //             method: 'emkan_insights.emkan_insights.doctype.external_email_template.external_email_template.sync_email_template',
            //             args: {
            //                 source_doctype: 'External Email Template',
            //                 names
            //             },
            //             freeze: true,
            //             freeze_message: __('Syncing {0} selected {1} record(s)...', [names.length, 'External Email Template']),
            //             callback(r) {
            //                 if (!r.exc) {
            //                     frappe.msgprint(__('Sync completed successfully'));
            //                     listview.refresh();
            //                 }
            //             }
            //         });
            //     }
            // );
        });
    }
};
            