// Copyright (c) 2026, Mukesh Variyani and contributors
// For license information, please see license.txt

frappe.ui.form.on("External SAP Configuration", {
	refresh(frm) {
		const grid = frm.fields_dict.mappings.grid;
		grid.update_docfield_property("fetch_data", "label", __("Fetch Data"));
		grid.update_docfield_property("sync_data", "hidden", 1);
		if (!frm.is_new()) {
			frm.add_custom_button(__("Fetch Data"), () => frm.trigger("fetch_configuration"));
			frm.add_custom_button(__("Fetch and Sync"), () => frm.trigger("fetch_and_sync_configuration"));
			frm.add_custom_button(__("Stop Sync and Clear Locks"), () => frm.trigger("stop_synchronization"));
		}
	},
	stop_synchronization(frm) {
		frappe.confirm(
			__(
				"Stop the running and queued SAP jobs for {0} and clear its mapping locks? A job that is stopped part way is rolled back to its last commit and starts again on the next run.",
				[frm.doc.company || frm.doc.name],
			),
			() => {
				frappe.call({
					method: "emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.stop_synchronization",
					args: { name: frm.doc.name },
					freeze: true,
					freeze_message: __("Stopping SAP jobs..."),
					callback(response) {
						const result = response.message || {};
						const lines = [
							__("Running jobs stopped: {0}", [(result.stopped || []).length]),
							__("Queued jobs cancelled: {0}", [(result.cancelled || []).length]),
							__("Locks cleared: {0}", [result.locks_cleared || 0]),
						];
						if ((result.left_running || []).length) {
							lines.push(
								__("Left running because they also cover other companies: {0}", [
									result.left_running.join(", "),
								]),
							);
						}
						frappe.msgprint({ title: __("SAP Synchronization"), message: lines.join("<br>"), indicator: "green" });
						frm.reload_doc();
					},
				});
			},
		);
	},
	fetch_and_sync_configuration(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the SAP configuration before fetching and synchronizing data."));
			return;
		}

		frappe.confirm(
			__("Fetch the latest SAP data and synchronize it to ERPNext?"),
			() => {
				frappe.call({
					method: "emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.fetch_and_sync_configuration",
					args: { name: frm.doc.name },
					callback(response) {
						const status = response.message?.status;
						if (status) {
							frappe.show_alert({
								message: __("SAP fetch and sync for {0}: {1}.", [frm.doc.company, status]),
								indicator: status === "queued" ? "green" : "orange",
							});
						}
					},
				});
			},
		);
	},
	fetch_configuration(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the SAP configuration before fetching data."));
			return;
		}

		frappe.call({
			method: "emkan_insights.sap.doctype.external_sap_configuration.external_sap_configuration.fetch_configuration",
			args: { name: frm.doc.name },
			callback(response) {
				const status = response.message?.status;
				if (status) {
					frappe.show_alert({
						message: __("SAP data fetch for {0}: {1}.", [frm.doc.company, status]),
						indicator: status === "queued" ? "green" : "orange",
					});
				}
			},
		});
	},
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
			callback(response) {
				const result = response.message || {};
				frappe.show_alert({
					message: __("SAP fetch for {0}: {1}.", [row.sap_mapping, result.status || "queued"]),
					indicator: result.status === "queued" ? "green" : "orange",
				});
			},
		});
	},
});
