# Copyright (c) 2026, Gseven Computer Business and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WhatsAppConnectionUser(Document):
	"""One row per ERPNext user who logs in from the WhatsApp ERP Connector app:
	their WhatsApp number, the computer and the access token issued at login.
	Rows are created and updated by whatsapp_connector.api.login."""

	def on_trash(self):
		# Deleting the row logs that computer out (WhatsApp unlinked, chats removed).
		if self.device and self.access_token:
			from whatsapp_connector.api.login import revoke_device

			revoke_device(self.device, reason="connection_deleted", update_connection=False)
