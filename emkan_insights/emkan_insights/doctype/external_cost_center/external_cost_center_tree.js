frappe.provide("frappe.treeview_settings");

frappe.treeview_settings["External Cost Center"] = {

    breadcrumb: "Accounts",

    root_label: "External Cost Centers",

    get_tree_root: false,

    get_tree_nodes:
        "emkan_insights.emkan_insights.doctype.external_cost_center.external_cost_center.get_external_cost_center_children",

    add_tree_node:
        "emkan_insights.emkan_insights.doctype.external_cost_center.external_cost_center.add_external_cost_center",

    filters: [
        {
            fieldname: "company",
            fieldtype: "Select",
            options: erpnext.utils.get_tree_options("company"),
            label: __("Company"),
            default: erpnext.utils.get_tree_default("company"),
        },
    ],

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
            fieldname: "cost_center_name",
            label: __("New Cost Center Name"),
            reqd: true,
        },
        {
            fieldtype: "Check",
            fieldname: "is_group",
            label: __("Is Group"),
            description: __(
                "Further cost centers can be made under Groups but entries can be made against non-Groups"
            ),
        },
        {
            fieldtype: "Data",
            fieldname: "cost_center_number",
            label: __("Cost Center Number"),
            description: __(
                "Number of new Cost Center, it will be included in the cost center name as a prefix"
            ),
        },
    ],

    ignore_fields: ["parent_cost_center"],

    onload: function (treeview) {

        function get_company() {
            return treeview.page.fields_dict.company.get_value();
        }

    },
};