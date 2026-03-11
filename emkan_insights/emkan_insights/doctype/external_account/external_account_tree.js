frappe.provide("frappe.treeview_settings");

frappe.treeview_settings["External Account"] = {
	breadcrumb: "Accounts",
	title: __("Chart of External Accounts"),
	get_tree_root: false,
	filters: [
		{
			fieldname: "company",
			fieldtype: "Select",
			options: erpnext.utils.get_tree_options("company"),
			label: __("Company"),
			default: erpnext.utils.get_tree_default("company"),
			on_change: function () {
				var me = frappe.treeview_settings["External Account"].treeview;
				var company = me.page.fields_dict.company.get_value();
				if (!company) {
					frappe.throw(__("Please set a Company"));
				}
				frappe.call({
					method: "erpnext.accounts.doctype.account.account.get_root_company",
					args: {
						company: company,
					},
					callback: function (r) {
						if (r.message) {
							let root_company = r.message.length ? r.message[0] : "";
							me.page.fields_dict.root_company.set_value(root_company);

							frappe.db.get_value(
								"Company",
								{ name: company },
								"allow_account_creation_against_child_company",
								(r) => {
									frappe.flags.ignore_root_company_validation =
										r.allow_account_creation_against_child_company;
								}
							);
						}
					},
				});
			},
		},
		{
			fieldname: "root_company",
			fieldtype: "Data",
			label: __("Root Company"),
			hidden: true,
			disable_onchange: true,
		},
	],
	root_label: "Accounts",
	get_tree_nodes:
		"emkan_insights.emkan_insights.doctype.external_account.external_account.get_external_account_children",
	add_tree_node:
		"emkan_insights.emkan_insights.doctype.external_account.external_account.add_external_account",
	menu_items: [
		{
			label: __("New Company"),
			action: function () {
				frappe.new_doc("Company", true);
			},
			condition: 'frappe.boot.user.can_create.indexOf("Company") !== -1',
		},
	],
	fields: [
		{
			fieldtype: "Data",
			fieldname: "remote_id",
			label: __("Remote ID"),
			reqd: true,
		},
		{
			fieldtype: "Data",
			fieldname: "account_name",
			label: __("New Account Name"),
			reqd: true,
		},
		{
			fieldtype: "Data",
			fieldname: "account_number",
			label: __("Account Number"),
		},
		{
			fieldtype: "Check",
			fieldname: "is_group",
			label: __("Is Group"),
			onchange: function () {
				if (!this.value) {
					this.layout.set_value("root_type", "");
				}
			},
		},
		{
			fieldtype: "Select",
			fieldname: "root_type",
			label: __("Root Type"),
			options: ["Asset", "Liability", "Equity", "Income", "Expense"].join("\n"),
			depends_on: "eval:doc.is_group && !doc.parent_account",
		},
		{
			fieldtype: "Select",
			fieldname: "account_type",
			label: __("Account Type"),
			options: frappe
				.get_meta("External Account")
				.fields.filter((d) => d.fieldname == "account_type")[0].options,
		},
		{
			fieldtype: "Float",
			fieldname: "tax_rate",
			label: __("Tax Rate"),
			depends_on: 'eval:doc.is_group==0&&doc.account_type=="Tax"',
		},
		{
			fieldtype: "Link",
			fieldname: "account_currency",
			label: __("Currency"),
			options: "Currency",
		},
		{
			fieldtype: "Link",
			fieldname: "source_site",
			label: __("Source Site"),
			options: "External Site Configuration",
		},
	],
	ignore_fields: ["parent_account"],
	onload: function (treeview) {
		frappe.treeview_settings["External Account"].treeview = {};
		$.extend(frappe.treeview_settings["External Account"].treeview, treeview);
		function get_company() {
			return treeview.page.fields_dict.company.get_value();
		}

		// treeview.page.add_inner_button(
		// 	__("Chart of Cost Centers"),
		// 	function () {
		// 		frappe.set_route("Tree", "Cost Center", { company: get_company() });
		// 	},
		// 	__("View")
		// );

		// treeview.page.add_inner_button(
		// 	__("Opening Invoice Creation Tool"),
		// 	function () {
		// 		frappe.set_route("Form", "Opening Invoice Creation Tool", { company: get_company() });
		// 	},
		// 	__("View")
		// );

		// treeview.page.add_inner_button(
		// 	__("Period Closing Voucher"),
		// 	function () {
		// 		frappe.set_route("List", "Period Closing Voucher", { company: get_company() });
		// 	},
		// 	__("View")
		// );

		// treeview.page.add_inner_button(
		// 	__("Journal Entry"),
		// 	function () {
		// 		frappe.new_doc("Journal Entry", { company: get_company() });
		// 	},
		// 	__("Create")
		// );

		// treeview.page.add_inner_button(
		// 	__("Company"),
		// 	function () {
		// 		frappe.new_doc("Company");
		// 	},
		// 	__("Create")
		// );

		for (let report of [
			"Trial Balance",
			"General Ledger",
			"Balance Sheet",
			"Profit and Loss Statement",
			"Cash Flow",
			"Accounts Payable",
			"Accounts Receivable",
		]) {
			// treeview.page.add_inner_button(
			// 	__(report),
			// 	function () {
			// 		frappe.set_route("query-report", report, { company: get_company() });
			// 	},
			// 	__("Financial Statements")
			// );
		}
	},
	post_render: function (treeview) {
		frappe.treeview_settings["External Account"].treeview["tree"] = treeview.tree;
		if (treeview.can_create) {
			treeview.page.set_primary_action(
				__("New"),
				function () {
					let root_company = treeview.page.fields_dict.root_company.get_value();
					if (root_company) {
						frappe.throw(__("Please add the account to root level Company - {0}"), [
							root_company,
						]);
					} else {
						treeview.new_node();
					}
				},
				"add"
			);
		}
	},
	toolbar: [
		{
			label: __("Add Child"),
			condition: function (node) {
				return (
					frappe.boot.user.can_create.indexOf("External Account") !== -1 &&
					(!frappe.treeview_settings["External Account"].treeview.page.fields_dict.root_company.get_value() ||
						frappe.flags.ignore_root_company_validation) &&
					node.expandable &&
					!node.hide_add
				);
			},
			click: function () {
				var me = frappe.views.trees["External Account"];
				me.new_node();
			},
			btnClass: "hidden-xs",
		},
	],
	extend_toolbar: true,
};
