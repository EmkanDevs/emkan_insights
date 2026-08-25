# import frappe
# from frappe.contacts.doctype.contact.contact import Contact as _Contact

# class Contact(_Contact):
# 	def autoname(self):
# 		# self.name = self._get_full_name()

# 		# concat party name if reqd
# 		if self.remote_id:
# 			self.name = self.remote_id

# 		else:
# 			for link in self.links:
# 				self.name = self.name + "-" + link.link_name.strip()
# 				break

# 			if frappe.db.exists("Contact", self.name):
# 				self.name = append_number_if_name_exists("Contact", self.name)

import frappe
from frappe.contacts.doctype.contact.contact import Contact as _Contact

class Contact(_Contact):

	def autoname(self):

		if self.remote_id:
			self.name = self.remote_id
			return

		self.name = (self.full_name or "").strip()

		if not self.name:
			self.name = " ".join(
				part for part in [self.first_name, self.middle_name, self.last_name] if part
			).strip()

		for link in self.links:
			if not self.name and link.link_name:
				self.name = link.link_name.strip()
				break

		if not self.name:
			self.name = self.email_id or self.user or frappe.generate_hash(length=10)

		if frappe.db.exists("Contact", self.name):
			from frappe.model.naming import append_number_if_name_exists
			self.name = append_number_if_name_exists("Contact", self.name)
