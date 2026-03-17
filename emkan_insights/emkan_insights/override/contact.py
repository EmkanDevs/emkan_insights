import frappe
from frappe.contacts.doctype.contact.contact import Contact as _Contact

class Contact(_Contact):
	def autoname(self):
		# self.name = self._get_full_name()

		# concat party name if reqd
		if self.remote_id:
			self.name = self.remote_id

		else:
			for link in self.links:
				self.name = self.name + "-" + link.link_name.strip()
				break

			if frappe.db.exists("Contact", self.name):
				self.name = append_number_if_name_exists("Contact", self.name)
