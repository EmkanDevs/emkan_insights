// Copyright (c) 2026, Mukesh Variyani and contributors
// For license information, please see license.txt

frappe.ui.form.on("External Account", {
	account_number(frm) {
		set_naming_series(frm);
	},
	account_name(frm) {
		set_naming_series(frm);
	},
	company(frm) {
		set_naming_series(frm);
	},
});

function set_naming_series(frm) {
	const company = frm.doc.company;
	const accountNumber = (frm.doc.account_number || "").trim();
	const accountName = (frm.doc.account_name || "").trim();

	if (!company) {
		frm.set_value("naming_series", [accountNumber, accountName].filter(Boolean).join("-"));
		return;
	}

	frappe.db.get_value("Company", company, "abbr").then((r) => {
		const companyAbbr = (r && r.message && r.message.abbr ? r.message.abbr : "").trim();
		const namingSeries = [accountNumber, accountName, companyAbbr].filter(Boolean).join("-");
		frm.set_value("naming_series", namingSeries);
	});
}
