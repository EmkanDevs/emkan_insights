frappe.ui.form.on('External Site Configuration', {
    site_name: function (frm) {  
        set_naming_series(frm);
    },

    site_url: function (frm) {
        set_naming_series(frm);
    },
    // // refresh: function(frm) {
    // //     frm.add_custom_button(__('Load Doctype Mappings'), function () {
    // //         load_fetch_data_from_site_config(frm);
    // //     }, __('Actions'));
    // },
    after_save: function(frm) {
        // ✅ Only reload to show rows inserted by Python — no data fetching here
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



frappe.ui.form.on('External Site Configuration CT', {
    // Trigger when Ref Doctype is selected/changed in the child table
    ref_doctype: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        const mapping = get_mapping();

        if (row.ref_doctype && mapping[row.ref_doctype]) {
            frappe.model.set_value(cdt, cdn, 'exported_doctype', mapping[row.ref_doctype]);
        } else {
            frappe.model.set_value(cdt, cdn, 'exported_doctype', "");
        }
    },

    fetch_data: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];

        if (!row.ref_doctype) {
            frappe.msgprint(__('Please select a DocType first.'));
            return;
        }

        // Double check mapping before calling backend
        if (!row.exported_doctype) {
            const mapping = get_mapping();
            frappe.model.set_value(cdt, cdn, 'exported_doctype', mapping[row.ref_doctype]);
        }

        const generic_method = "emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.sync_data_from_remote";
        const base_args = {
            site_url: frm.doc.site_url,
            api_key: frm.doc.api_key,
            api_secret: frm.doc.api_secret,
            child_docname: cdn,
            company: frm.doc.company,
            configuration_name: frm.doc.name
        };
        const generic_args = Object.assign({}, base_args, { ref_doctype: row.ref_doctype });

        const show_result = function (r) {
            if (r.message) {
                let count = r.message;
                let errors = [];
                let missing_parents = [];
                let fetched_total = null;
                let not_saved_count = null;

                if (typeof r.message === 'object') {
                    if (r.message.last_sync) {
                        frappe.model.set_value(cdt, cdn, 'last_sync', r.message.last_sync);
                    }
                    count          = r.message.count;
                    errors         = r.message.errors || [];
                    missing_parents = r.message.missing_parent_accounts || [];
                    fetched_total  = r.message.fetched_total ?? null;
                    not_saved_count = r.message.not_saved_count ?? null;
                }

                // ── Summary alert ─────────────────────────────────────────────
                let summary = __(`Done! Saved ${count} records.`);
                if (fetched_total !== null) {
                    summary = __(`Done! Fetched ${fetched_total}, saved ${count}, not saved ${not_saved_count}.`);
                }
                frappe.show_alert({ message: summary, indicator: not_saved_count > 0 ? 'orange' : 'green' });

                // ── General errors ────────────────────────────────────────────
                if (errors.length) {
                    const lines = errors.slice(0, 10).map(err => {
                        const ref = err.ref_doctype ? `${err.ref_doctype}` : 'Doc';
                        const id = err.remote_id ? err.remote_id : `Row ${err.row_index || '?'}`;
                        return `<li><b>${ref} → ${id}</b><br>${err.error}</li>`;
                    });
                    const more = errors.length > 10
                        ? `<li><i>...and ${errors.length - 10} more errors</i></li>`
                        : '';
                    frappe.msgprint({
                        title: __('Some records failed'),
                        indicator: 'orange',
                        message: `<ul>${lines.join('')}${more}</ul>`
                    });
                }

                // ── Missing / stubbed parent accounts report ──────────────────
                if (missing_parents.length) {
                    show_missing_parents_report(missing_parents, row.ref_doctype, fetched_total);
                }

                frm.refresh_field('external_site_configuration_ct');
                frm.refresh();
            }
        };

        const call_sync = function (method, args) {
            frappe.call({
                method: method,
                args: args,
                freeze: true,
                freeze_message: __(`Fetching ${row.ref_doctype}s...`),
                callback: show_result,
                error: function () {}
            });
        };

        call_sync(generic_method, generic_args);
    }
});


// ─────────────────────────────────────────────────────────────────────────────
// Missing Parent Accounts Report Dialog
// ─────────────────────────────────────────────────────────────────────────────

function show_missing_parents_report(missing_parents, ref_doctype, fetched_total) {
    const rows_html = missing_parents.map((entry, i) => {
        return `
            <tr style="vertical-align:top; border-bottom:1px solid #eee;">
                <td style="padding:6px 8px; color:#888;">${i + 1}</td>
                <td style="padding:6px 8px; font-family:monospace;">${entry.account_id || '—'}</td>
                <td style="padding:6px 8px;">${entry.account_name || '—'}</td>
                <td style="padding:6px 8px; font-family:monospace; color:#d97706;">${entry.missing_parent_id || '—'}</td>
                <td style="padding:6px 8px; color:#555; font-size:0.85em;">${entry.reason || '—'}</td>
                <td style="padding:6px 8px; color:#0369a1; font-size:0.85em;">${entry.action_taken || '—'}</td>
            </tr>
        `;
    }).join('');

    const fetched_label = fetched_total !== null ? ` out of <b>${fetched_total}</b> fetched` : '';
    const table_html = `
        <p style="margin-bottom:8px;">
            <b>${missing_parents.length}</b> account(s)${fetched_label} were <b>not saved</b>
            during the <b>${ref_doctype}</b> sync.
            Accounts that had parent issues but <em>still saved successfully</em> are <b>not shown here</b>.
        </p>
        <div style="overflow-x:auto; max-height:400px; overflow-y:auto;">
            <table style="width:100%; border-collapse:collapse; font-size:0.9em;">
                <thead>
                    <tr style="background:#f3f4f6; text-align:left;">
                        <th style="padding:6px 8px;">#</th>
                        <th style="padding:6px 8px;">Account ID (Remote)</th>
                        <th style="padding:6px 8px;">Account Name</th>
                        <th style="padding:6px 8px;">Missing Parent ID</th>
                        <th style="padding:6px 8px;">Reason</th>
                        <th style="padding:6px 8px;">Action Taken</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows_html}
                </tbody>
            </table>
        </div>
        <p style="margin-top:10px; font-size:0.85em; color:#888;">
            Tip: If parent accounts belong to a different company or were deleted on the remote site,
            they will never appear in this sync. In that case, re-check the remote Chart of Accounts
            and ensure all parent accounts are accessible via the API.
        </p>
    `;

    // Use a frappe Dialog so the user can scroll and copy data
    const d = new frappe.ui.Dialog({
        title: __(`Accounts Not Saved (${missing_parents.length} of ${fetched_total !== null ? fetched_total : '?'} fetched)`),
        size: 'extra-large',
        fields: [
            {
                fieldtype: 'HTML',
                fieldname: 'report_html',
                options: table_html
            }
        ],
        primary_action_label: __('Copy as CSV'),
        primary_action: function () {
            const csv_header = 'Account ID,Account Name,Missing Parent ID,Reason,Action Taken\n';
            const csv_rows = missing_parents.map(e => {
                const escape = (v) => `"${(v || '').replace(/"/g, '""')}"`;
                return [
                    escape(e.account_id),
                    escape(e.account_name),
                    escape(e.missing_parent_id),
                    escape(e.reason),
                    escape(e.action_taken)
                ].join(',');
            }).join('\n');

            const blob = new Blob([csv_header + csv_rows], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `missing_parent_accounts_${frappe.datetime.now_datetime().replace(/[: ]/g, '-')}.csv`;
            a.click();
            URL.revokeObjectURL(url);
            frappe.show_alert({ message: __('CSV downloaded'), indicator: 'green' });
        },
        secondary_action_label: __('Close'),
        secondary_action: function () { d.hide(); }
    });

    d.show();
}


// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

function set_naming_series(frm) {
    if (frm.doc.site_name && frm.doc.site_url) {
        let series = `${frm.doc.site_name}-${frm.doc.site_url}.`;
        frm.set_value('naming_series', series);
    }
}

function get_mapping() {
    return {
        "Account": "External Account",
        "Address": "External Address",
        "Asset": "External Asset",
        "Asset Category": "External Asset Category",
        "Bank": "External Bank",
        "Bank Account": "External Bank Account",
        "Contact": "External Contact",
        "Contract": "External Contract",
        "Cost Center": "External Cost Center",
        "Customer": "External Customer",
        "Item": "External Item",
        "Item Group": "External Item Group",
        "Location": "External Location",
        "Manufacturer": "External Manufacturer",
        "Price List": "External Price List",
        "Project Type": "External Project Type",
        "Supplier": "External Supplier",
        "Supplier Group": "External Supplier Group",
        "Territory": "External Territory",
        "UOM": "External Uom",
        "Warehouse": "External Warehouse",
        "Purchase Invoice": "External Purchase Invoice",
        "Payment Entry": "External Payment Entry",
        "Purchase Order" : "External Purchase Order",
        "Sales Order": "External Sales Order",
        "Stock Entry": "External Stock Entry",
        "Project" : "External Project",
        "Request for Quotation": "External Request for Quotation",
        "Supplier Quotation": "External Supplier Quotation",
        "Purchase Receipt": "External Purchase Receipt",
        "Quotation": "External Quotation",
        "Delivery Note": "External Delivery Note",
        "Sales Invoice": "External Sales Invoice",
        "Sales Taxes and Charges Template": "External Sales Taxes and Charges Template",
        "Purchase Taxes and Charges Template": "External Purchase Taxes and Charges Template",
        "Letter Head": "External Letter Head",
        "Expense Claim": "External Expense Claim",
        "Payment Terms Template": "External Payment Terms Template",
        "Sales Person": "External Sales Person",
        "Terms and Conditions": "External Terms and Conditions",
        "Journal Entry": "External Journal Entry",
        "Material Request" : "External Material Request",
        "Lead" : "External Lead",
        "Opportunity" : "External Opportunity"

    };

}
// ─────────────────────────────────────────────────────────────────────────────
// ✅ NEW — Load rows from Site Configuration Doctypes CT
// ─────────────────────────────────────────────────────────────────────────────

// function load_fetch_data_from_site_config(frm) {
//     if (frm._fetch_in_progress) return;  // guard against double call
//     frm._fetch_in_progress = true;

//     frappe.call({
//         method: 'emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.get_site_config_doctypes_rows',
//         callback: function(res) {
//             frm._fetch_in_progress = false;

//             if (!res.message || res.message.length === 0) {
//                 frappe.show_alert({
//                     message: __('No rows found in Site Configuration Doctypes CT.'),
//                     indicator: 'orange'
//                 });
//                 return;
//             }

//             frm.clear_table('fetch_data');

//             res.message.forEach(function(row) {
//                 let child = frm.add_child('fetch_data');
//                 child.ref_doctype = row.ref_doctype;
//                 child.exported_doctype = row.exported_doctype;
//             });

//             frm.refresh_field('fetch_data');
//             frappe.show_alert({
//                 message: __(res.message.length + ' rows loaded successfully.'),
//                 indicator: 'green'
//             });
//         },
//         error: function() {
//             frm._fetch_in_progress = false;  // always reset on error too
//         }
//     });
// }
function load_fetch_data_from_site_config(frm) {
    if (frm._fetch_in_progress) return;
    frm._fetch_in_progress = true;

    frappe.call({
        method: 'emkan_insights.emkan_insights.doctype.external_site_configuration.external_site_configuration.get_site_config_doctypes_rows',
        callback: function(res) {
            frm._fetch_in_progress = false;

            if (!res.message || res.message.length === 0) {
                frappe.show_alert({
                    message: __('No rows found in Site Configuration Doctypes CT.'),
                    indicator: 'orange'
                });
                return;
            }

            frm.clear_table('fetch_data');
            res.message.forEach(function(row) {
                let child = frm.add_child('fetch_data');
                child.ref_doctype = row.ref_doctype;
                child.exported_doctype = row.exported_doctype;
            });
            frm.refresh_field('fetch_data');
            // ✅ NO frm.save() here — that was causing the loop
            frappe.show_alert({
                message: __(res.message.length + ' rows loaded successfully.'),
                indicator: 'green'
            });
        },
        error: function() {
            frm._fetch_in_progress = false;
        }
    });
}