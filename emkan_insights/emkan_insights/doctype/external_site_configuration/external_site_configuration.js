frappe.ui.form.on('External Site Configuration', {
    refresh: function(frm) {
        add_bulk_insert_listener(frm);
        add_fetch_listener(frm);
        if (!frm.is_new()) {
            add_sequence_button(
                frm,
                __('Fetch Only'),
                "emkan_insights.emkan_insights.scheduler.fetch_external_site_configuration_sequence",
                __('Queueing sequential fetch...')
            );
            add_sequence_button(
                frm,
                __('Fetch + Bulk Insert'),
                "emkan_insights.emkan_insights.scheduler.fetch_and_sync_external_site_configuration_sequence",
                __('Queueing sequential fetch and bulk insert...')
            );
            add_bulk_insert_button(frm);
            add_sequence_button(
                frm,
                __('Stop Jobs'),
                "emkan_insights.emkan_insights.scheduler.stop_external_site_configuration_jobs",
                __('Stopping external sync jobs...')
            );
        }
    },

    after_save: function(frm) {
        frm.reload_doc();
    },

    check_connection: function (frm) {
        if (!frm.doc.site_url || !frm.doc.api_key || !frm.doc.api_secret) {
            frappe.msgprint(__('Please enter all credentials first.'));
            return;
        }

        let base_url = frm.doc.site_url.replace(/\/$/, "");

        fetch(`${base_url}/api/method/frappe.auth.get_logged_user`, {
            method: 'GET',
            headers: {
                'Authorization': `token ${frm.doc.api_key}:${frm.doc.api_secret}`,
                'Content-Type': 'application/json'
            }
        })
            .then(response => {
                if (response.ok) {
                    frappe.msgprint({
                        title: __('Connection Successful'),
                        indicator: 'green',
                        message: __('Successfully connected to ') + base_url
                    });
                } else {
                    throw new Error(__('Unauthorized or Invalid URL'));
                }
            })
            .catch(err => {
                frappe.msgprint({
                    title: __('Connection Failed'),
                    indicator: 'red',
                    message: __('Could not connect. Check keys and ensure CORS is enabled on the target site.')
                });
            });
    },
});

function add_sequence_button(frm, label, method, freeze_message) {
    frm.add_custom_button(label, function () {
        frappe.call({
            method: method,
            args: {
                site_config: frm.doc.name
            },
            freeze: true,
            freeze_message: freeze_message,
            callback: function (r) {
                if (r.message) {
                    frappe.show_alert({
                        message: __(r.message.message || r.message.status || 'Done'),
                        indicator: get_sequence_indicator(r.message.status)
                    });
                    frm.reload_doc();
                }
            }
        });
    }, __('Sequential Sync'));
}

function get_sequence_indicator(status) {
    if (status === 'complete' || status === 'queued' || status === 'stopped') {
        return 'green';
    }
    if (status === 'complete_with_link_mismatches' || status === 'skipped') {
        return 'orange';
    }
    return 'blue';
}

function add_bulk_insert_button(frm) {
    frm.add_custom_button(__('Bulk Insert'), function () {
        if (frm.is_dirty()) {
            frappe.msgprint(__('Save the configuration and its DocType order before starting bulk insert.'));
            return;
        }

        const rows = (frm.doc.fetch_data || []).filter(row => row.ref_doctype && row.name);
        if (!rows.length) {
            frappe.msgprint(__('Add and save at least one Fetch Data row first.'));
            return;
        }

        queue_bulk_insert(frm.doc.name);
    }, __('Sequential Sync'));
}

function queue_bulk_insert(configuration_name) {
    frappe.call({
        method: "emkan_insights.emkan_insights.external_data_bulk_insert.queue_external_data_bulk_insert",
        args: { configuration_name: configuration_name },
        freeze: true,
        freeze_message: __("Queueing bulk insert..."),
        callback: function (r) {
            if (r.message) {
                frappe.show_alert({
                    message: __(r.message.message || "Bulk insert queued."),
                    indicator: "blue"
                });
            }
        }
    });
}

frappe.ui.form.on('External Site Configuration CT', {
    fetch_data: function (frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (frm.is_dirty()) {
            frappe.msgprint(__('Save the configuration before fetching data.'));
            return;
        }
        if (!row.ref_doctype || !row.exported_doctype) {
            frappe.msgprint(__('Select both the source and staging DocTypes first.'));
            return;
        }

        frappe.call({
            method: 'emkan_insights.emkan_insights.external_data_fetch.queue_external_data_fetch',
            args: { child_docname: cdn },
            freeze: true,
            freeze_message: __(`Queueing ${row.ref_doctype} fetch...`),
            callback: function (response) {
                if (response.message) {
                    frappe.show_alert({
                        message: __(response.message.message || 'Fetch queued.'),
                        indicator: 'blue'
                    });
                }
            }
        });
    }
});


function add_bulk_insert_listener(frm) {
    if (frm._bulk_insert_listener_registered) {
        return;
    }

    frappe.realtime.on("external_data_bulk_insert_complete", function (result) {
        if (result.configuration !== frm.doc.name) {
            return;
        }

        frappe.msgprint({
            title: __("Bulk Insert Complete"),
            indicator: "green",
            message: __(
                "Processed {0} DocTypes. Inserted {1} records and updated {2} existing records.",
                [result.processed_doctypes, result.inserted, result.updated]
            )
        });
    });
    frm._bulk_insert_listener_registered = true;
}

function add_fetch_listener(frm) {
    if (frm._external_fetch_listener_registered) {
        return;
    }

    frappe.realtime.on("external_data_fetch_complete", function (result) {
        const rows = frm.doc.fetch_data || [];
        if (!rows.some(row => row.name === result.child_docname)) {
            return;
        }

        frappe.show_alert({
            message: __("Fetched {0} {1} record(s).", [result.count, result.ref_doctype]),
            indicator: "green"
        });
        frm.reload_doc();
    });
    frm._external_fetch_listener_registered = true;
}
