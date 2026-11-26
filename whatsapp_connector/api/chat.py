"""Data API for the WhatsApp Chat desk page (/app/whatsapp-chat).

A conversation is (WhatsApp Account, conversation_key): the person's phone number
when known (so phone-number and privacy-ID/@lid messages of one person are one
conversation), otherwise the chat JID; for groups the group JID. Reactions are stored as their
own WhatsApp Message rows (message_type "reaction", quoted_message_id = target)
and are folded into the target message here.
"""

import json
import re

import frappe
from frappe import _
from frappe.utils import cint

from whatsapp_connector.access import allowed_accounts, check_account, publish_chat_event
from whatsapp_connector.api.messaging import SENDER_ROLES, validate_chat_jid
from whatsapp_connector.utils import normalize_phone, retry_on_conflict

MESSAGE_FIELDS = [
	"name",
	"direction",
	"status",
	"message_type",
	"content",
	"media_file",
	"media_filename",
	"media_mimetype",
	"media_size",
	"sender_name",
	"sender_jid",
	"phone",
	"whatsapp_message_id",
	"quoted_message_id",
	"message_timestamp",
	"creation",
	"error",
	"reference_doctype",
	"reference_name",
	"owner",
	"context_json",
]

MEDIA_TYPES = {"image", "video", "audio", "document", "sticker"}


def _media_note(m, context):
	"""State of a media message without a file in ERPNext (shown instead of the file)."""
	if m.media_file or m.message_type not in MEDIA_TYPES:
		return None
	if context.get("media_request") == "pending":
		return "requested"  # the user's computer is downloading it
	if context.get("media_request") == "failed" or context.get("media_error"):
		return "failed"
	if context.get("media_skipped") == "too_large":
		return "too_large"
	if context.get("media_on_request") or context.get("media_skipped") == "history":
		return "on_request"  # click Download to fetch it
	if m.direction == "Incoming":
		return "pending"  # automatic mode: the computer is still uploading it
	return "missing"


def _parse_context(value):
	try:
		return json.loads(value or "{}") or {}
	except ValueError:
		return {}


def _require_chat_user():
	if not SENDER_ROLES.intersection(frappe.get_roles()):
		frappe.throw(_("You do not have access to WhatsApp Chat."), frappe.PermissionError)


PHONE_KEY = re.compile(r"^\d{7,15}$")


def validate_conversation(conversation):
	conversation = (conversation or "").strip()
	if PHONE_KEY.match(conversation):
		return conversation
	return validate_chat_jid(conversation)


def send_target(conversation):
	"""Where replies go: the phone number when known, else the chat itself."""
	return f"{conversation}@s.whatsapp.net" if PHONE_KEY.match(conversation) else conversation


def _account_filter(account):
	if account and not frappe.db.exists("WhatsApp Account", account):
		frappe.throw(_("WhatsApp Account {0} not found.").format(account))
	if account:
		check_account(account)
	return account


@frappe.whitelist()
def get_accounts():
	_require_chat_user()
	allowed = allowed_accounts()
	if allowed is not None and not allowed:
		return []
	accounts = frappe.get_all(
		"WhatsApp Account",
		filters={"name": ("in", allowed)} if allowed is not None else None,
		fields=["name", "label", "phone_number", "status", "device", "is_default", "user"],
		order_by="is_default desc, label asc",
	)
	names = {
		u.name: u.full_name or u.name
		for u in frappe.get_all("User", filters={"name": ("in", list({a.user for a in accounts if a.user}) or [""])}, fields=["name", "full_name"])
	}
	devices = {
		d.name: d
		for d in frappe.get_all("WhatsApp Device", fields=["name", "device_name", "connection_status", "status"])
	}
	for a in accounts:
		device = devices.get(a.device) or {}
		a.device_name = device.get("device_name")
		a.device_online = device.get("connection_status") == "Online" and device.get("status") == "Approved"
		a.user_name = names.get(a.user) or a.user
		a.linked = a.status != "logged_out"
	# A link that never got a number (QR not scanned) and holds no chats is noise.
	with_chats = set(
		frappe.get_all("WhatsApp Message", filters={"account": ("in", [a.name for a in accounts if not a.phone_number] or [""])}, pluck="account", distinct=True)
	)
	accounts = [a for a in accounts if a.phone_number or a.name in with_chats or a.status in ("qr", "connecting", "connected")]
	# Linked numbers first, then those kept after logout / unlinking.
	accounts.sort(key=lambda a: (not a.linked, not a.is_default))
	return accounts


@frappe.whitelist()
def get_conversations(account=None, search=None, limit=50, offset=0):
	_require_chat_user()
	account = _account_filter(account)
	limit = min(max(cint(limit) or 50, 1), 200)
	conditions = ["m.conversation_key is not null"]
	values = {"limit": limit, "offset": max(cint(offset), 0)}
	if account:
		conditions.append("m.account = %(account)s")
		values["account"] = account
	else:
		allowed = allowed_accounts()
		if allowed is not None:
			if not allowed:
				return []
			conditions.append("m.account in %(allowed)s")
			values["allowed"] = tuple(allowed)
	if search:
		values["search"] = f"%{search.strip()}%"
		conditions.append(
			"""m.conversation_key in (select s.conversation_key from `tabWhatsApp Message` s
			where s.phone like %(search)s or s.sender_name like %(search)s
			   or s.content like %(search)s or s.chat_jid like %(search)s)"""
		)

	rows = frappe.db.sql(
		f"""select m.account, m.conversation_key as conversation,
			max(coalesce(m.message_timestamp, m.creation)) as last_at,
			sum(case when m.direction = 'Incoming' and m.is_read = 0 and m.message_type != 'reaction'
			    then 1 else 0 end) as unread
		from `tabWhatsApp Message` m
		where {" and ".join(conditions)}
		group by m.account, m.conversation_key
		order by last_at desc
		limit %(limit)s offset %(offset)s""",
		values,
		as_dict=True,
	)

	for row in rows:
		last = frappe.db.sql(
			"""select direction, status, message_type, content, media_filename
			from `tabWhatsApp Message` where account = %s and conversation_key = %s
			order by coalesce(message_timestamp, creation) desc, creation desc limit 1""",
			(row.account, row.conversation),
			as_dict=True,
		)[0]
		row.last = last
		row.is_group = row.conversation.endswith("@g.us")
		row.phone = row.conversation if PHONE_KEY.match(row.conversation) else None
		row.title = _chat_title(row.account, row.conversation)
	return rows


def _chat_title(account, conversation):
	from whatsapp_connector.contacts import display_name

	synced = display_name(account, conversation)
	if synced and not synced.startswith("+"):
		return synced
	if conversation.endswith("@g.us"):
		return _("Group {0}").format(conversation.split("@")[0][-6:])
	name = frappe.db.get_value(
		"WhatsApp Message",
		{"account": account, "conversation_key": conversation, "direction": "Incoming", "sender_name": ("is", "set")},
		"sender_name",
		order_by="creation desc",
	)
	if name:
		return name
	return f"+{conversation}" if PHONE_KEY.match(conversation) else conversation.split("@")[0]


@frappe.whitelist()
def get_messages(account, conversation, before=None, limit=40):
	"""Messages of one conversation, oldest first, with reactions folded in."""
	_require_chat_user()
	_account_filter(account)
	conversation = validate_conversation(conversation)
	limit = min(max(cint(limit) or 40, 1), 200)

	filters = [
		["account", "=", account],
		["conversation_key", "=", conversation],
		["message_type", "!=", "reaction"],
	]
	order = "coalesce(message_timestamp, creation)"
	conditions = ["account = %(account)s", "conversation_key = %(conversation)s", "message_type != 'reaction'"]
	values = {"account": account, "conversation": conversation, "limit": limit}
	if before:
		conditions.append(f"{order} < %(before)s")
		values["before"] = before
	messages = frappe.db.sql(
		f"""select {", ".join(f"`{f}`" for f in MESSAGE_FIELDS)}, {order} as sort_at
		from `tabWhatsApp Message` where {" and ".join(conditions)}
		order by {order} desc, creation desc limit %(limit)s""",
		values,
		as_dict=True,
	)
	messages.reverse()

	wa_ids = [m.whatsapp_message_id for m in messages if m.whatsapp_message_id]
	reactions = _reactions_for(account, wa_ids)
	quoted = _quoted_previews(account, [m.quoted_message_id for m in messages if m.quoted_message_id])
	for m in messages:
		m.reactions = reactions.get(m.whatsapp_message_id, [])
		m.quoted = quoted.get(m.quoted_message_id)
		context = _parse_context(m.pop("context_json", None))
		m.media_note = _media_note(m, context)
		m.media_error = context.get("media_error") if m.media_note == "failed" else None
		if m.media_note and context.get("thumbnail"):
			m.thumbnail = f"data:image/jpeg;base64,{context['thumbnail']}"

	return {
		"messages": messages,
		"has_more": len(messages) == limit,
		"title": _chat_title(account, conversation),
		"phone": conversation if PHONE_KEY.match(conversation) else None,
		"is_group": conversation.endswith("@g.us"),
		"send_to": send_target(conversation),
	}


def _reactions_for(account, wa_ids):
	"""{target wa id: [{emoji, count, mine, pending}]} - latest reaction per person wins."""
	if not wa_ids:
		return {}
	rows = frappe.get_all(
		"WhatsApp Message",
		filters={"account": account, "message_type": "reaction", "quoted_message_id": ("in", wa_ids)},
		fields=["quoted_message_id", "direction", "sender_jid", "phone", "content", "status"],
		order_by="creation asc",
	)
	latest = {}  # (target, reactor) -> row
	for r in rows:
		if r.direction == "Outgoing" and r.status in ("Failed", "Cancelled"):
			continue
		reactor = "me" if r.direction == "Outgoing" else (r.sender_jid or r.phone or "?")
		latest[(r.quoted_message_id, reactor)] = r

	out = {}
	for (target, reactor), r in latest.items():
		if not r.content:
			continue  # reaction removed
		bucket = out.setdefault(target, {})
		entry = bucket.setdefault(r.content, {"emoji": r.content, "count": 0, "mine": False, "pending": False})
		entry["count"] += 1
		if reactor == "me":
			entry["mine"] = True
			entry["pending"] = r.status in ("Queued", "Sending")
	return {target: list(bucket.values()) for target, bucket in out.items()}


def _quoted_previews(account, wa_ids):
	if not wa_ids:
		return {}
	rows = frappe.get_all(
		"WhatsApp Message",
		filters={"account": account, "whatsapp_message_id": ("in", wa_ids)},
		fields=["whatsapp_message_id", "direction", "content", "media_filename", "message_type", "sender_name"],
	)
	return {r.whatsapp_message_id: r for r in rows}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def request_media(message):
	"""Download button: ask the computer of this WhatsApp to fetch the file from
	WhatsApp and upload it. Waits there if that computer is offline."""
	from whatsapp_connector.api.device import _set_media_request
	from whatsapp_connector.utils import EVENT_JOBS_AVAILABLE, notify_device

	_require_chat_user()
	frappe.has_permission("WhatsApp Message", "read", message, throw=True)
	doc = frappe.get_doc("WhatsApp Message", message)
	check_account(doc.account)
	if doc.message_type not in MEDIA_TYPES:
		frappe.throw(_("This message has no media."))
	if doc.media_file:
		return {"name": doc.name, "media_file": doc.media_file}
	if not doc.whatsapp_message_id:
		frappe.throw(_("This message has not reached WhatsApp."))
	_set_media_request(doc, "pending")
	doc.save(ignore_permissions=True)
	device = frappe.db.get_value("WhatsApp Account", doc.account, "device")
	online = frappe.db.get_value("WhatsApp Device", device, "connection_status") == "Online" if device else False
	if device:
		notify_device(device, EVENT_JOBS_AVAILABLE)
	return {"name": doc.name, "requested": True, "computer_online": online}


@frappe.whitelist(methods=["POST"])
def mark_read(account, conversation):
	_require_chat_user()
	_account_filter(account)
	conversation = validate_conversation(conversation)
	try:
		_mark_read(account, conversation)
	except frappe.QueryDeadlockError:
		# Still colliding with a sync after retries: unread counts are refreshed
		# next time the chat is opened, never worth an error dialog.
		frappe.db.rollback()
		frappe.clear_messages()
		return {"ok": False}
	return {"ok": True}


@retry_on_conflict
def _mark_read(account, conversation):
	frappe.db.sql(
		"""update `tabWhatsApp Message` set is_read = 1
		where account = %s and conversation_key = %s and direction = 'Incoming' and is_read = 0""",
		(account, conversation),
	)


@frappe.whitelist()
def search_contacts(txt=None, account=None, limit=20):
	"""New Chat: WhatsApp contacts and groups of one number, for the phone field's
	dropdown. value = phone digits, or the chat ID when there is no phone number
	(groups, privacy-ID contacts)."""
	_require_chat_user()
	if not account:
		return []
	_account_filter(account)
	limit = min(max(cint(limit) or 20, 1), 50)
	conditions = ["account = %(account)s"]
	values = {"account": account, "limit": limit}
	txt = (txt or "").strip()
	if txt:
		digits = re.sub(r"\D", "", txt)
		conditions.append(
			"(display_name like %(txt)s or saved_name like %(txt)s or push_name like %(txt)s"
			+ (" or phone like %(digits)s" if digits else "")
			+ ")"
		)
		values.update({"txt": f"%{txt}%", "digits": f"%{digits}%"})
	rows = frappe.db.sql(
		f"""select display_name, phone, jid, is_group from `tabWhatsApp Contact`
		where {" and ".join(conditions)}
		order by (saved_name is null), display_name limit %(limit)s""",
		values,
		as_dict=True,
	)
	out = []
	for r in rows:
		if r.is_group:
			out.append({"value": r.jid, "label": r.display_name or _("Group"), "description": _("Group")})
		elif r.phone:
			out.append({"value": r.phone, "label": r.display_name or f"+{r.phone}", "description": f"+{r.phone}"})
		elif r.jid:
			out.append({"value": r.jid, "label": r.display_name or r.jid, "description": _("Number hidden by WhatsApp")})
	return out


@frappe.whitelist()
def resolve_chat(phone):
	"""Phone number typed by a user -> chat JID for a new conversation."""
	_require_chat_user()
	number = normalize_phone(phone)
	return {"conversation": number, "chat_jid": f"{number}@s.whatsapp.net", "phone": number}


def publish_chat_update(doc, method=None):
	"""doc_events hook: refresh open WhatsApp Chat pages (ids only, no content)."""
	if not doc.conversation_key or doc.flags.get("skip_chat_update"):
		return
	# Only to users allowed to see this account (owner of its computer + managers).
	publish_chat_event(
		doc.account,
		{"account": doc.account, "conversation": doc.conversation_key, "name": doc.name, "direction": doc.direction},
		after_commit=True,
	)
