import frappe
from erpnext.crm.doctype.lead.lead import Lead as _Lead


class Lead(_Lead):

    def _fix_contact_primary_flags(self):
        """
        Ensure only one email and one phone row is marked as primary
        on self.contact_doc before it is saved. Prevents Frappe's
        set_primary_email() from throwing when external sync data
        has multiple is_primary=1 rows.
        """
        contact = getattr(self, "contact_doc", None)
        if not contact:
            return

        # Fix email_ids — keep only first is_primary=1
        if contact.get("email_ids"):
            found = False
            for row in contact.email_ids:
                if row.get("is_primary"):
                    if found:
                        row.is_primary = 0
                    else:
                        found = True
            if not found and contact.email_ids:
                contact.email_ids[0].is_primary = 1

        # Fix phone_nos — keep only first of each primary type
        if contact.get("phone_nos"):
            found_phone = False
            found_mobile = False
            for row in contact.phone_nos:
                if row.get("is_primary_phone"):
                    if found_phone:
                        row.is_primary_phone = 0
                    else:
                        found_phone = True
                if row.get("is_primary_mobile_no"):
                    if found_mobile:
                        row.is_primary_mobile_no = 0
                    else:
                        found_mobile = True

    def link_to_contact(self):
        """
        Override of ERPNext's link_to_contact.
        Uses getattr() for all Lead fields so this works regardless of
        which fields exist in the v15 Lead doctype.
        Injects _fix_contact_primary_flags() before contact_doc.save().
        """
        lead_name = self.get("lead_name") or self.get("first_name") or ""

        # ── Check if contact already linked ──────────────────────────────
        if lead_name:
            contact_names = frappe.get_all(
                "Contact",
                filters={"first_name": lead_name},
                pluck="name",
            )
            for name in contact_names:
                contact = frappe.get_doc("Contact", name)
                for link in contact.links:
                    if link.link_doctype == "Lead" and link.link_name == self.name:
                        self.contact_doc = contact
                        return

        # ── Build new Contact — safe getattr for every field ─────────────
        contact = frappe.new_doc("Contact")
        contact.first_name      = lead_name
        contact.salutation      = self.get("salutation")
        contact.company_name    = self.get("company_name")
        contact.gender          = self.get("gender")
        contact.designation     = self.get("job_title") or self.get("designation")
        contact.status          = "Open"
        contact.unsubscribed    = self.get("unsubscribed") or 0

        email_id = self.get("email_id")
        if email_id:
            contact.append("email_ids", {"email_id": email_id, "is_primary": 1})

        phone = self.get("phone")
        if phone:
            contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})

        mobile_no = self.get("mobile_no")
        if mobile_no:
            contact.append(
                "phone_nos",
                {"phone": mobile_no, "is_primary_mobile_no": 1},
            )

        contact.append(
            "links",
            {"link_doctype": "Lead", "link_name": self.name},
        )

        contact.flags.ignore_permissions = True
        contact.flags.ignore_mandatory   = True

        self.contact_doc = contact

        # ✅ Fix duplicate primary flags BEFORE save
        self._fix_contact_primary_flags()

        self.contact_doc.save()