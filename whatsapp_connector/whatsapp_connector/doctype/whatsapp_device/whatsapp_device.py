# Copyright (c) 2026, Gseven Computer Business and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from whatsapp_connector.utils import EVENT_DEVICE_STATUS, notify_device


class WhatsAppDevice(Document):
	def on_update(self):
		if self.has_value_changed("status"):
			# Tell the desktop app immediately that it was approved or disabled.
			notify_device(self.name, EVENT_DEVICE_STATUS, {"status": self.status})
			if self.status == "Approved":
				notify_device(self.name)

	def on_trash(self):
		if frappe.db.exists("WhatsApp Account", {"device": self.name}):
			frappe.throw(
				frappe._("Disable this device instead of deleting it: it has WhatsApp accounts and message history.")
			)
