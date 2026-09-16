frappe.listview_settings['External Site Configuration'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Fetch + Sync All Companies'), () => {
            frappe.call({
                method: 'emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.fetch_and_sync_all_companies',
                freeze: true,
                freeze_message: __('Queueing fetch and sync for all companies...'),
                callback(r) {
                    if (!r.message) {
                        return;
                    }

                    frappe.show_alert({
                        message: __(get_scheduler_message(r.message.status)),
                        indicator: r.message.status === 'queued' ? 'green' : 'orange'
                    });
                }
            });
        });
    }
};

function get_scheduler_message(status) {
    if (status === 'already_queued') {
        return 'Fetch and sync is already queued for all companies.';
    }
    return 'Fetch and sync queued for all companies.';
}
