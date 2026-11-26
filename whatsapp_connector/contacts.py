"""WhatsApp contacts synced from the desktop connector.

One WhatsApp Contact per person (or group) per WhatsApp account. WhatsApp may
identify the same person by phone number (…@s.whatsapp.net) or by privacy ID
(…@lid); when both become known they are merged into one record and the person's
older @lid messages are moved into their phone-number conversation.

ERPNext's Contact DocType is never read during sync (it can be large) and never
written; the optional "ERPNext Contact" link on a WhatsApp Contact is set by hand.
"""

import re

import frappe
from frappe.utils import now_datetime


JID_RE = re.compile(r"^[0-9A-Za-z._:-]{1,80}@(s\.whatsapp\.net|g\.us|lid)$")
FIELDS = ("saved_name", "push_name", "verified_name")


def _clip(value, length=140):
	return str(value).strip()[:length] if value not in (None, "") else None


def _digits(value):
	digits = re.sub(r"\D", "", str(value or ""))
	return digits if 7 <= len(digits) <= 15 else None


def _find(account, jid, lid, phone):
	for filters in (
		{"account": account, "jid": jid},
		{"account": account, "lid": lid} if lid else None,
		{"account": account, "jid": lid} if lid else None,
		{"account": account, "phone": phone, "is_group": 0} if phone else None,
	):
		if filters:
			name = frappe.db.get_value("WhatsApp Contact", filters)
			if name:
				return name
	return None


def upsert_contact(account, item):
	"""item: {jid, lid, phone, name, notify, verified_name, is_group}. Fields that are
	missing/empty never overwrite known values (WhatsApp sends partial updates)."""
	jid = _clip(item.get("jid"), 100)
	if not jid or not JID_RE.match(jid):
		return None
	is_group = jid.endswith("@g.us")
	lid = _clip(item.get("lid"), 100)
	lid = lid if lid and lid.endswith("@lid") else (jid if jid.endswith("@lid") else None)
	phone = None if is_group else (_digits(item.get("phone")) or (_digits(jid.split("@")[0]) if jid.endswith("@s.whatsapp.net") else None))
	if phone and not is_group:
		jid = f"{phone}@s.whatsapp.net"  # prefer the phone-number identity

	values = {
		"saved_name": _clip(item.get("name")),
		"push_name": _clip(item.get("notify")),
		"verified_name": _clip(item.get("verified_name")),
	}

	name = _find(account, jid, lid, phone)
	doc = frappe.get_doc("WhatsApp Contact", name) if name else frappe.new_doc("WhatsApp Contact")
	if not name:
		doc.account = account
	doc.jid = jid
	doc.is_group = 1 if is_group else 0
	if lid:
		doc.lid = lid
	if phone:
		doc.phone = phone
	for field, value in values.items():
		if value:
			doc.set(field, value)
	doc.last_synced = now_datetime()


	doc.save(ignore_permissions=True) if name else doc.insert(ignore_permissions=True)

	if doc.phone and doc.lid:
		merge_lid_messages(account, doc.lid, doc.phone)
	return doc.name


def merge_lid_messages(account, lid, phone):
	"""Messages of an @lid chat that were stored before its phone number was known."""
	frappe.db.sql(
		"""update `tabWhatsApp Message`
		set conversation_key = %(phone)s, phone = ifnull(phone, %(phone)s)
		where account = %(account)s and chat_jid = %(lid)s and is_group = 0
		  and (conversation_key = %(lid)s or conversation_key is null or phone is null)""",
		{"account": account, "lid": lid, "phone": phone},
	)
	from whatsapp_connector.whatsapp_connector.doctype.whatsapp_message.whatsapp_message import fill_message_numbers

	fill_message_numbers(account)


def phone_for_lid(account, lid):
	if not lid:
		return None
	return frappe.db.get_value(
		"WhatsApp Contact", {"account": account, "lid": lid, "phone": ("is", "set")}, "phone"
	)


def display_name(account, conversation):
	"""Best name for a conversation key (phone digits or JID), or None."""
	filters = (
		{"account": account, "phone": conversation, "is_group": 0}
		if conversation.isdigit()
		else {"account": account, "jid": conversation}
	)
	row = frappe.db.get_value("WhatsApp Contact", filters, ["display_name", "contact"], as_dict=True)
	if not row:
		return None
	if row.contact:
		full_name = frappe.db.get_value("Contact", row.contact, "full_name")
		if full_name:
			return full_name
	return row.display_name
