frappe.listview_settings['External Bank Account'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Bank Account'), () => {
            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Bank Account'));
                return;
            }

            const names = selected.map(row => row.name);

            const dialog = new frappe.ui.Dialog({
                title: __('Select Company'),
                fields: [
                    {
                        label: __('Company'),
                        fieldname: 'company',
                        fieldtype: 'Link',
                        options: 'Company',
                        reqd: 1
                    }
                ],
                primary_action_label: __('Sync'),
                primary_action(values) {

                    frappe.confirm(
                        __('Sync selected External Bank Account records to Bank Account master for company {0}?', [values.company]),
                        () => {
                            frappe.call({
                                method: 'emkan_insights.emkan_insights.external_sync.sync_external_docs',
                                args: {
                                    source_doctype: 'External Bank Account',
                                    names: names,
                                    company: values.company
                                },
                                freeze: true,
                                freeze_message: __('Syncing {0} selected records...', [names.length]),
                                callback(r) {
                                    if (!r.exc) {
                                        frappe.show_alert({
                                            message: __('Bank Account synced successfully'),
                                            indicator: 'green'
                                        });

                                        dialog.hide();
                                        listview.refresh();
                                    }
                                }
                            });
                        }
                    );
                }
            });

            dialog.show();
        });
    }
};