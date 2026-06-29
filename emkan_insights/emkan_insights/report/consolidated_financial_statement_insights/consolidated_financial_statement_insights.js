// Copyright (c) 2026, Mukesh Variyani and contributors
// For license information, please see license.txt

frappe.query_reports["Consolidated Financial Statement Insights"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "filter_based_on",
			label: __("Filter Based On"),
			fieldtype: "Select",
			options: ["Fiscal Year", "Date Range"],
			default: ["Fiscal Year"],
			reqd: 1,
			on_change: function () {
				let filter_based_on = frappe.query_report.get_filter_value("filter_based_on");
				frappe.query_report.toggle_filter_display(
					"from_fiscal_year",
					filter_based_on === "Date Range"
				);
				frappe.query_report.toggle_filter_display("to_fiscal_year", filter_based_on === "Date Range");
				frappe.query_report.toggle_filter_display(
					"period_start_date",
					filter_based_on === "Fiscal Year"
				);
				frappe.query_report.toggle_filter_display(
					"period_end_date",
					filter_based_on === "Fiscal Year"
				);

				frappe.query_report.refresh();
			},
		},
		{
			fieldname: "period_start_date",
			label: __("Start Date"),
			fieldtype: "Date",
			hidden: 1,
			reqd: 1,
		},
		{
			fieldname: "period_end_date",
			label: __("End Date"),
			fieldtype: "Date",
			hidden: 1,
			reqd: 1,
		},
		{
			fieldname: "from_fiscal_year",
			label: __("Start Year"),
			fieldtype: "Link",
			options: "Fiscal Year",
			default: erpnext.utils.get_fiscal_year(frappe.datetime.get_today()),
			reqd: 1,
			on_change: () => {
				frappe.model.with_doc(
					"Fiscal Year",
					frappe.query_report.get_filter_value("from_fiscal_year"),
					function (r) {
						let year_start_date = frappe.model.get_value(
							"Fiscal Year",
							frappe.query_report.get_filter_value("from_fiscal_year"),
							"year_start_date"
						);
						frappe.query_report.set_filter_value({
							period_start_date: year_start_date,
                        });
					}
				);
			},
		},
		{
			fieldname: "to_fiscal_year",
			label: __("End Year"),
			fieldtype: "Link",
			options: "Fiscal Year",
			default: erpnext.utils.get_fiscal_year(frappe.datetime.get_today()),
			reqd: 1,
			on_change: () => {
				frappe.model.with_doc(
					"Fiscal Year",
					frappe.query_report.get_filter_value("to_fiscal_year"),
					function (r) {
						let year_end_date = frappe.model.get_value(
                            "Fiscal Year",
                            frappe.query_report.get_filter_value("to_fiscal_year"),
                            "year_end_date"
                        );
                        frappe.query_report.set_filter_value({
                            period_end_date: year_end_date,
                        });
                    }
                );
            },
        },
        {
            fieldname: "finance_book",
            label: __("Finance Book"),
            fieldtype: "Link",
            options: "Finance Book",
        },
        {
            fieldname: "report",
            label: __("Report"),
            fieldtype: "Select",
            options: ["Profit and Loss Statement", "Balance Sheet", "Cash Flow"],
            default: "Balance Sheet",
            reqd: 1,
        },
        {
            fieldname: "presentation_currency",
            label: __("Currency"),
            fieldtype: "Select",
            options: erpnext.get_presentation_currency_list(),
            default: frappe.defaults.get_user_default("Currency"),
        },
        {
            fieldname: "accumulated_in_group_company",
            label: __("Accumulated Values in Group Company"),
            fieldtype: "Check",
            default: 0,
        },
        {
            fieldname: "include_default_book_entries",
            label: __("Include Default FB Entries"),
            fieldtype: "Check",
            default: 1,
        },
        {
            fieldname: "show_zero_values",
            label: __("Show zero values"),
            fieldtype: "Check",
        },
    ],
    formatter: function (value, row, column, data, default_formatter) {
        if (data && column.fieldname == "account") {
            value = data.account_name || value;

            column.link_onclick =
                "erpnext.financial_statements.open_general_ledger(" + JSON.stringify(data) + ")";
            column.is_tree = true;
        }

        if (data && data.account && column.apply_currency_formatter) {
            data.currency = erpnext.get_currency(column.company_name);
        }

        value = default_formatter(value, row, column, data);
        if (data && !data.parent_account) {
            value = $(`<span>${value}</span>`);

            var $value = $(value).css("font-weight", "bold");

            value = $value.wrap("<p></p>").parent().html();
        }
        return value;
    },
    onload: function (report) {
        let fiscal_year = erpnext.utils.get_fiscal_year(frappe.datetime.get_today());

        frappe.model.with_doc("Fiscal Year", fiscal_year, function (r) {
            var fy = frappe.model.get_doc("Fiscal Year", fiscal_year);
            frappe.query_report.set_filter_value({
                period_start_date: fy.year_start_date,
                period_end_date: fy.year_end_date,
            });
        });

        // Add "Create JV" button next to Actions
        report.page.add_inner_button(__("Create JV"), function () {
            let company = frappe.query_report.get_filter_value("company");
            let report_type = frappe.query_report.get_filter_value("report");
            let accumulated_in_group = frappe.query_report.get_filter_value("accumulated_in_group_company");

            // Only allow when Report Type is Profit and Loss Statement AND Accumulated Values in Group Company is checked
            if (report_type !== "Profit and Loss Statement" || !accumulated_in_group) {
                frappe.msgprint(__("Create JV is only available when Report Type is 'Profit and Loss Statement' and 'Accumulated Values in Group Company' is enabled"));
                return;
            }

            // Get the report data
            let data = frappe.query_report.data || [];

            // Fetch company custom accounts first, then find the matching amount
            frappe.db.get_doc("Company", company).then(company_doc => {
                let income_account = company_doc.custom_income_account;
                let expense_account = company_doc.custom_expense_account;

                if (!income_account || !expense_account) {
                    frappe.msgprint(__("Please set Custom Income Account and Custom Expense Account in Company {0}", [company]));
                    return;
                }

                // Find the amount from the report for the income account
                // Use row.account (actual docname) NOT row.account_name (display name)
                let amount = 0;
                
                data.forEach(row => {
                    // Match against row.account which is the actual Account docname (e.g., "Internal Sales - EHC")
                    if (row.account && row.account === income_account) {
                        // Get the value from the main company column
                        amount = flt(row[company]);
                    }
                });

                if (amount === 0) {
                    frappe.msgprint(__("Could not find amount for account {0} in the report", [income_account]));
                    return;
                }

                let abs_amount = Math.abs(amount);
                let accounts = [];

                if (amount <= 0) {
                    // Positive amount: Income Credit, Expense Debit
                    accounts.push({
                        account: income_account,
                        credit_in_account_currency: abs_amount,
                        debit_in_account_currency: 0
                    });
                    accounts.push({
                        account: expense_account,
                        debit_in_account_currency: abs_amount,
                        credit_in_account_currency: 0
                    });
                } else {
                    // Negative amount: Income Debit, Expense Credit
                    accounts.push({
                        account: income_account,
                        debit_in_account_currency: abs_amount,
                        credit_in_account_currency: 0
                    });
                    accounts.push({
                        account: expense_account,
                        credit_in_account_currency: abs_amount,
                        debit_in_account_currency: 0
                    });
                }

                // Create Journal Entry
                frappe.call({
                    method: "frappe.client.insert",
                    args: {
                        doc: {
                            doctype: "Journal Entry",
                            company: company,
                            posting_date: frappe.datetime.get_today(),
                            voucher_type: "Journal Entry",
                            accounts: accounts,
                            remarks: __("JV from Consolidated P&L - Account: {0}, Amount: {1}", [income_account, amount])
                        }
                    },
                    callback: function(r) {
                        if (r.message) {
                            frappe.set_route("Form", "Journal Entry", r.message.name);
                            frappe.show_alert({
                                message: __("Journal Entry {0} created successfully", [r.message.name]),
                                indicator: "green"
                            });
                        }
                    }
                });
            });
        });
    },
};