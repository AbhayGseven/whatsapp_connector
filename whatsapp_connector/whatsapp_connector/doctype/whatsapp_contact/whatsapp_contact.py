# Copyright (c) 2026, Gseven Computer Business and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WhatsAppContact(Document):
	def validate(self):
		self.display_name = (
			self.saved_name
			or self.verified_name
			or self.push_name
			or (f"+{self.phone}" if self.phone else None)
			or (self.jid or "").split("@")[0]
		)[:140]
