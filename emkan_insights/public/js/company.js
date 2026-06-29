frappe.ui.form.on("Company",{
    refresh:function(frm) {
        frm.set_query("custom_income_account", function () {
			return {
				filters: { is_group: 0 ,company : frm.doc.name},
			};
		});
        frm.set_query("custom_expense_account", function () {
			return {
				filters: { is_group: 0 ,company : frm.doc.name},
			};
		});
        frm.set_query("custom_receivable_account", function () {
			return {
				filters: { is_group: 0 ,company : frm.doc.name},
			};
		});
        frm.set_query("custom_payable_account", function () {
			return {
				filters: { is_group: 0 ,company : frm.doc.name},
			};
		});
    }
})