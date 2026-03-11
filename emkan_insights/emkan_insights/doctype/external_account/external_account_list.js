frappe.listview_settings['External Account'] = {
    onload(listview) {

        // ✅ BUTTON 1: Sync Selected to Account
        listview.page.add_inner_button(__('Sync to Account'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Account'));
                return;
            }

            const names = selected.map(row => row.name);

            const dialog = new frappe.ui.Dialog({
                title: __('Sync External Accounts'),
                fields: [
                    {
                        label: __('Company'),
                        fieldname: 'company',
                        fieldtype: 'Link',
                        options: 'Company',
                        reqd: 1,
                        default: frappe.defaults.get_default('company')
                    }
                ],
                primary_action_label: __('Sync'),
                primary_action(values) {

                    frappe.call({
                        method: 'emkan_insights.emkan_insights.accounts.sync_account_docs',
                        args: {
                            source_doctype: 'External Account',
                            names: names,
                            company: values.company
                        },
                        freeze: true,
                        freeze_message: __('Syncing {0} selected record(s)...', [names.length]),
                        callback(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: __('Accounts synced successfully'),
                                    indicator: 'green'
                                });
                                dialog.hide();
                                listview.refresh();
                            }
                        }
                    });
                }
            });

            dialog.show();
        });

    }
};