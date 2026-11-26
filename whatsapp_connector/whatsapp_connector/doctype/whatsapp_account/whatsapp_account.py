# Copyright (c) 2026, Gseven Computer Business and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WhatsAppAccount(Document):
	def on_update(self):
		# Only one default sending account.
		if self.is_default and self.has_value_changed("is_default"):
			frappe.db.set_value(
				"WhatsApp Account",
				{"is_default": 1, "name": ("!=", self.name)},
				"is_default",
				0,
				update_modified=False,
			)
