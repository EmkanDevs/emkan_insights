frappe.ui.form.on('Global Entity Mapping', {

    // 🔄 When parent fields change → update all child rows
    entity_type(frm) {
        update_child_fields(frm);
    },

    key_field(frm) {
        update_child_fields(frm);
    }

});


frappe.ui.form.on('Global Entity Mapping Item', {

    // ➕ When new row added → auto-fill
    mapping_add(frm, cdt, cdn) {
        let row = locals[cdt][cdn];

        row.entity_type = frm.doc.entity_type;

        frm.refresh_field('mapping');
    }

});


// 🔧 Common function
function update_child_fields(frm) {
    (frm.doc.mapping || []).forEach(row => {
        row.entity_type = frm.doc.entity_type;
    });

    frm.refresh_field('mapping');
}