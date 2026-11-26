"""Fill WhatsApp Message.from_number / to_number for messages stored before
these fields existed."""

import frappe


def execute():
	from whatsapp_connector.whatsapp_connector.doctype.whatsapp_message.whatsapp_message import (
		fill_message_numbers,
	)

	for account in frappe.get_all("WhatsApp Account", pluck="name"):
		fill_message_numbers(account)
