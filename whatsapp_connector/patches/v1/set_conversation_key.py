"""Backfill WhatsApp Message.conversation_key so that a person addressed both by
phone number and by privacy ID (@lid) appears as one conversation."""

import frappe

from whatsapp_connector.whatsapp_connector.doctype.whatsapp_message.whatsapp_message import (
	compute_conversation_key,
)


def execute():
	# Phones learned per (account, @lid chat), from any message that carried one.
	learned = {}
	for row in frappe.db.sql(
		"""select account, chat_jid, phone from `tabWhatsApp Message`
		where chat_jid like '%%@lid' and ifnull(phone, '') != '' and is_group = 0
		order by creation desc""",
		as_dict=True,
	):
		learned.setdefault((row.account, row.chat_jid), row.phone)

	for row in frappe.get_all(
		"WhatsApp Message", fields=["name", "account", "chat_jid", "phone", "is_group"], filters={"chat_jid": ("is", "set")}
	):
		key = compute_conversation_key(row.account, row.chat_jid, row.phone, row.is_group)
		if not row.is_group and key == row.chat_jid and (row.account, row.chat_jid) in learned:
			key = learned[(row.account, row.chat_jid)]
		frappe.db.set_value("WhatsApp Message", row.name, "conversation_key", key, update_modified=False)
