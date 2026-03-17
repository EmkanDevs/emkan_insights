frappe.listview_settings['External Purchase Invoice'] = {

    onload(listview) {

        // ===============================
        // ✅ BUTTON 1 : Sync to Purchase Invoice
        // ===============================

        listview.page.add_inner_button(__('Sync to Purchase Invoice'), () => {

            const selected = listview.get_checked_items();

            if (!selected.length) {
                frappe.msgprint(__('Please select at least one External Purchase Invoice'));
                return;
            }

            const names = selected.map(row => row.name);

            frappe.call({
                method:
                    'emkan_insights.emkan_insights.external_pi_sync.sync_external_purchase_invoice_docs',

                args: {
                    source_doctype: 'External Purchase Invoice',
                    names: names
                },

                freeze: true,
                freeze_message: __(
                    'Syncing {0} Purchase Invoice(s)...',
                    [names.length]
                ),

                callback(r) {

                    if (!r.exc) {

                        frappe.show_alert({
                            message: __('Purchase Invoice synced successfully'),
                            indicator: 'green'
                        });

                        listview.refresh();
                    }
                }
            });

        });


        // ===============================
        // 🔥 BUTTON 2 : Fetch External Site
        // ===============================

        // listview.page.add_inner_button(__('Fetch from External Site'), () => {

        //     const dialog = new frappe.ui.Dialog({

        //         title: __('Fetch Purchase Invoices'),

        //         fields: [
        //             {
        //                 label: __('External Site Configuration'),
        //                 fieldname: 'config',
        //                 fieldtype: 'Link',
        //                 options: 'External Site Configuration',
        //                 reqd: 1
        //             }
        //         ],

        //         primary_action_label: __('Fetch'),

        //         primary_action(values) {

        //             frappe.call({

        //                 method:
        //                 'emkan_insights.emkan_insights.doctype.external_purchase_invoice.external_purchase_invoice.fetch_purchase_invoice_from_external',

        //                 args: {
        //                     config_name: values.config
        //                 },

        //                 freeze: true,

        //                 freeze_message:
        //                     __('Fetching Purchase Invoices...'),

        //                 callback(r) {

        //                     if (!r.exc) {

        //                         frappe.show_alert({

        //                             message:
        //                                 __('Purchase Invoices fetched successfully'),

        //                             indicator: 'green'
        //                         });

        //                         dialog.hide();

        //                         listview.refresh();
        //                     }
        //                 }
        //             });

        //         }
        //     });

        //     dialog.show();

        // });

    }
};