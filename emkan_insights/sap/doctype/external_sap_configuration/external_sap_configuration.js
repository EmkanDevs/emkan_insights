// Copyright (c) 2026, Mukesh Variyani and contributors
// For license information, please see license.txt

frappe.ui.form.on("External SAP Configuration", {
	check_connection(frm) {
		if (frm.is_new() || frm.is_dirty()) {
			frappe.msgprint(__("Save the SAP configuration before checking the connection."));
			return;
		}

		frappe.call({
			method: "emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.check_connection",
			args: { name: frm.doc.name },
			freeze: true,
			freeze_message: __("Checking SAP connection..."),
			callback(response) {
				if (response.message?.status === "ok") {
					frappe.show_alert({ message: response.message.message, indicator: "green" });
					frm.reload_doc();
				}
			},
		});
	},
});

frappe.ui.form.on("External Site Configuration CT", {
	fetch_data(frm, cdt, cdn) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the SAP configuration before fetching data."));
			return;
		}

		const row = locals[cdt][cdn];
		if (!row.sap_mapping) {
			frappe.msgprint(__("Select a SAP mapping first."));
			return;
		}

		frappe.call({
			method: "emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.fetch_mapping",
			args: { row_name: cdn },
			freeze: true,
			freeze_message: __("Fetching {0} from SAP...", [row.sap_mapping]),
			callback(response) {
				const result = response.message || {};
				frappe.msgprint(
					__("Created {0}, updated {1}, failed {2}.", [
						(result.created || []).length,
						(result.updated || []).length,
						(result.failed || []).length,
					])
				);
				frm.reload_doc();
			},
		});
	},

	sync_data(frm, cdt, cdn) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the SAP configuration before syncing data."));
			return;
		}

		const row = locals[cdt][cdn];
		frappe.call({
			method: "emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.queue_sync_mapping",
			args: { row_name: cdn },
			callback(response) {
				const result = response.message || {};
				frappe.show_alert({
					message: __("SAP sync for {0}: {1}.", [row.sap_mapping, result.status || "queued"]),
					indicator: result.status === "queued" ? "green" : "orange",
				});
			},
		});
	},
});
