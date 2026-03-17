frappe.listview_settings['External Sales Stage'] = {
    onload(listview) {
        listview.page.add_inner_button(__('Sync to Sales Stage'), () => {
        });
    }
};
