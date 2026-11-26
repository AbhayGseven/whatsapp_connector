# Copyright (c) 2026, Gseven Computer Business and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

INCOMING_STATUSES = {"Received"}
OUTGOING_STATUSES = {"Queued", "Sending", "Sent", "Delivered", "Read", "Failed", "Cancelled"}


def compute_conversation_key(account, chat_jid, phone, is_group=False):
	"""One key per person (or group) per account.

	WhatsApp may address the same person by phone number (…@s.whatsapp.net) in one
	message and by privacy ID (…@lid) in another, so the phone number is the key
	whenever it is known; for an @lid chat without a phone on this message, a phone
	learned from another message of the same chat is used.
	"""
	chat_jid = chat_jid or ""
	if is_group or chat_jid.endswith("@g.us"):
		return chat_jid or None
	if phone:
		return phone
	if chat_jid.endswith("@s.whatsapp.net"):
		return chat_jid.split("@")[0]
	if chat_jid and account:
		from whatsapp_connector.contacts import phone_for_lid

		known = phone_for_lid(account, chat_jid) or frappe.db.get_value(
			"WhatsApp Message",
			{"account": account, "chat_jid": chat_jid, "phone": ("is", "set"), "is_group": 0},
			"phone",
		)
		if known:
			return known
	return chat_jid or None


def _own_number_cache():
	cache = getattr(frappe.local, "wa_own_numbers", None)
	if cache is None:
		cache = frappe.local.wa_own_numbers = {}
	return cache


def own_number(account):
	"""This WhatsApp account's own number (cached per request)."""
	if not account:
		return None
	cache = _own_number_cache()
	if account not in cache:
		cache[account] = frappe.db.get_value("WhatsApp Account", account, "phone_number")
	return cache[account]


def message_numbers(direction, is_group, phone, chat_jid, own):
	"""(from_number, to_number) of a message. Groups have no number: the receiver
	of a group message is the group ID."""
	group = chat_jid if (is_group or (chat_jid or "").endswith("@g.us")) else None
	if direction == "Incoming":
		return phone or None, group or own or None
	return own or None, group or phone or None


def fill_message_numbers(account):
	"""Fill From/To on an account's messages once a missing number becomes known
	(this account's own number, or a contact's number behind a privacy ID)."""
	own = frappe.db.get_value("WhatsApp Account", account, "phone_number")
	_own_number_cache()[account] = own
	frappe.db.sql(
		"""update `tabWhatsApp Message` set
			from_number = coalesce(from_number,
				case when direction = 'Incoming' then nullif(phone, '') else %(own)s end),
			to_number = coalesce(to_number,
				case when is_group = 1 or chat_jid like '%%@g.us' then chat_jid
				     when direction = 'Incoming' then %(own)s else nullif(phone, '') end)
		where account = %(account)s and (from_number is null or to_number is null)""",
		{"account": account, "own": own},
	)


class WhatsAppMessage(Document):
	def has_value_changed_by_user(self):
		"""Saved messages are records: only the system changes them (status, receipts)."""
		before = self.get_doc_before_save()
		if not before:
			return False
		editable = ("account", "phone", "content", "media_file", "reference_doctype", "reference_name", "send_to_contact", "chat_jid")
		return any(self.get(f) != before.get(f) for f in editable)

	def before_validate(self):
		if self.is_new() and self._is_composed_in_desk():
			self._prepare_outgoing()

	def _is_composed_in_desk(self):
		"""A new message typed in the WhatsApp Message form (+ Add). Messages from the
		desktop app carry a dedupe key; queue_message / reactions set a command."""
		return not self.command and not self.dedupe_key and (self.direction or "Outgoing") == "Outgoing"

	def _prepare_outgoing(self):
		"""Same rules as sending from the chat page: own linked number, valid target,
		text and/or a file. The computer of that number then sends it."""
		from whatsapp_connector.access import allowed_accounts
		from whatsapp_connector.api.messaging import (
			_command_for,
			_require_sender,
			_resolve_account,
			_resolve_attachment,
			_target,
		)

		_require_sender()
		if self.send_to_contact and not (self.phone or self.chat_jid):
			contact = frappe.db.get_value(
				"WhatsApp Contact", self.send_to_contact, ["account", "phone", "jid", "is_group"], as_dict=True
			)
			if contact:
				self.account = self.account or contact.account
				if contact.phone and not contact.is_group:
					self.phone = contact.phone
				else:
					self.chat_jid = contact.jid
		if not (self.phone or self.chat_jid):
			frappe.throw(_("Enter the mobile number to send to, or choose a WhatsApp Contact."))
		if not (self.content or "").strip() and not self.media_file:
			frappe.throw(_("Write a message or attach a file."))

		phone, chat_jid = _target(to=None if self.chat_jid else self.phone, chat_jid=self.chat_jid)
		self.account = _resolve_account(self.account, allowed_accounts())
		self.device = frappe.db.get_value("WhatsApp Account", self.account, "device")
		self.update(
			{
				"direction": "Outgoing",
				"status": "Queued",
				"phone": phone,
				"chat_jid": chat_jid,
				"is_group": 1 if chat_jid.endswith("@g.us") else 0,
				"command": "SEND_TEXT",
				"message_type": "text",
				"idempotency_key": self.idempotency_key or frappe.generate_hash(length=32),
				"attempts": 0,
			}
		)
		if self.media_file:
			file_doc, mimetype = _resolve_attachment(self.media_file)
			self.command, self.message_type = _command_for(mimetype, self.content)
			self.media_filename = file_doc.file_name
			self.media_mimetype = mimetype
			self.media_size = cint(file_doc.file_size)
		self.flags.notify_device = self.device

	def validate(self):
		if not self.is_new() and not self.flags.ignore_permissions and self.has_value_changed_by_user():
			frappe.throw(_("A WhatsApp message cannot be edited after it was created."))
		self.from_number, self.to_number = message_numbers(
			self.direction, self.is_group, self.phone, self.chat_jid, own_number(self.account)
		)
		allowed = INCOMING_STATUSES if self.direction == "Incoming" else OUTGOING_STATUSES
		if self.status not in allowed:
			frappe.throw(_("Status {0} is not valid for an {1} message.").format(self.status, self.direction))
		if self.direction == "Outgoing" and not self.account:
			frappe.throw(_("Outgoing messages need a WhatsApp Account."))

	def before_insert(self):
		self.conversation_key = compute_conversation_key(self.account, self.chat_jid, self.phone, self.is_group)

	def after_insert(self):
		if self.flags.get("notify_device"):
			from whatsapp_connector.utils import notify_device

			notify_device(self.flags.notify_device)  # the computer picks it up right away
		# First time this privacy-ID chat reveals its phone number: move its earlier
		# messages (keyed by the @lid) into the phone-number conversation.
		if self.phone and not self.is_group and (self.chat_jid or "").endswith("@lid"):
			frappe.db.sql(
				"""update `tabWhatsApp Message` set conversation_key = %s, phone = ifnull(phone, %s)
				where account = %s and chat_jid = %s and conversation_key = %s""",
				(self.phone, self.phone, self.account, self.chat_jid, self.chat_jid),
			)
			fill_message_numbers(self.account)
