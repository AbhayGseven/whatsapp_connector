"""Business-facing API: queue WhatsApp messages from ERPNext.

From Python (server scripts excluded, use a custom app / hooks):

    from whatsapp_connector.api.messaging import queue_message
    queue_message("+91 98765 43210", "Your invoice is ready",
                  attachment=file_url, reference_doctype="Sales Invoice", reference_name=si.name)

From the desk / REST (WhatsApp User, WhatsApp Manager or System Manager):

    POST /api/method/whatsapp_connector.api.messaging.send
         {"to": "...", "message": "...", "attachment": "/private/files/x.pdf"}

The message is stored as an Outgoing "WhatsApp Message" with status Queued and the
owning desktop device is woken over realtime. ERPNext keeps it until the device
claims it, so sending works even when the computer is temporarily offline.
"""

import json
import mimetypes
import re

import frappe
from frappe import _
from frappe.utils import cint

from whatsapp_connector.utils import (
	retry_on_conflict,
	ROLE_MANAGER,
	ROLE_USER,
	get_settings,
	normalize_phone,
	notify_device,
)

SENDER_ROLES = {"System Manager", ROLE_MANAGER, ROLE_USER}

# Individual chats (phone number or WhatsApp privacy ID) and groups.
JID_RE = re.compile(r"^[0-9A-Za-z._:-]{1,80}@(s\.whatsapp\.net|g\.us|lid)$")
MAX_REACTION_LENGTH = 16


def validate_chat_jid(chat_jid):
	chat_jid = (chat_jid or "").strip()
	if not JID_RE.match(chat_jid):
		frappe.throw(_("{0} is not a valid WhatsApp chat.").format(frappe.bold(chat_jid)))
	return chat_jid


def _target(to=None, chat_jid=None):
	"""-> (phone or None, chat_jid). A chat JID wins over a phone number."""
	if chat_jid:
		chat_jid = validate_chat_jid(chat_jid)
		phone = chat_jid.split("@")[0] if chat_jid.endswith("@s.whatsapp.net") else None
		return (normalize_phone(phone, country_code="") if phone else None), chat_jid
	if not to:
		frappe.throw(_("Provide a phone number or a chat."))
	phone = normalize_phone(to)
	return phone, f"{phone}@s.whatsapp.net"


def _require_sender():
	if not SENDER_ROLES.intersection(frappe.get_roles()):
		frappe.throw(_("You are not allowed to send WhatsApp messages."), frappe.PermissionError)


def _resolve_account(account=None, allowed=None):
	"""allowed: None = any account, else the accounts the caller may use."""
	if account:
		name = frappe.db.get_value("WhatsApp Account", account) or frappe.db.get_value(
			"WhatsApp Account", {"phone_number": normalize_phone(account)}
		)
		if not name:
			frappe.throw(_("WhatsApp Account {0} not found.").format(account))
		if allowed is not None and name not in allowed:
			frappe.throw(_("You do not have access to this WhatsApp account."), frappe.PermissionError)
		if frappe.db.get_value("WhatsApp Account", name, "status") == "logged_out":
			frappe.throw(_("This WhatsApp is no longer linked to a computer. Its chats are kept for reference; link it again to send."))
		return name
	scope = {"name": ("in", allowed or [""])} if allowed is not None else {}
	scope["status"] = ("!=", "logged_out")
	name = frappe.db.get_value("WhatsApp Account", {**scope, "is_default": 1}) or frappe.db.get_value(
		"WhatsApp Account", {**scope, "status": "connected"}, order_by="modified desc"
	)
	if not name:
		frappe.throw(_("No WhatsApp account is available. Link one in the desktop connector first."))
	return name


def _resolve_attachment(attachment):
	# A File name is exact; a URL may be shared by several File records.
	file_name = (attachment if frappe.db.exists("File", attachment) else None) or frappe.db.get_value(
		"File", {"file_url": attachment}, "name"
	)
	if not file_name:
		frappe.throw(_("Attachment {0} not found.").format(attachment))
	file_doc = frappe.get_doc("File", file_name)
	limit = get_settings().max_media_size_mb * 1024 * 1024
	if cint(file_doc.file_size) > limit:
		frappe.throw(_("Attachment is larger than {0} MB.").format(get_settings().max_media_size_mb))
	mimetype = mimetypes.guess_type(file_doc.file_name)[0] or "application/octet-stream"
	return file_doc, mimetype


def _command_for(mimetype, caption=None):
	if mimetype.startswith("image/"):
		return "SEND_IMAGE", "image"
	if mimetype.startswith("video/"):
		return "SEND_VIDEO", "video"
	# WhatsApp audio has no caption: with a caption it is sent as a document.
	if mimetype.startswith("audio/") and not caption:
		return "SEND_AUDIO", "audio"
	if mimetype == "application/pdf":
		return "SEND_PDF", "document"
	return "SEND_DOCUMENT", "document"


def queue_message(
	to=None,
	message=None,
	attachment=None,
	account=None,
	reference_doctype=None,
	reference_name=None,
	idempotency_key=None,
	chat_jid=None,
	allowed_accounts=None,
):
	"""Queue an outgoing WhatsApp message (no permission check; for server code).

	to:               phone number in any common format
	chat_jid:         alternatively a WhatsApp chat (…@s.whatsapp.net, group …@g.us, or …@lid)
	message:          text, or the caption when an attachment is given
	attachment:       File URL or File name (image -> SEND_IMAGE, PDF -> SEND_PDF, other -> SEND_DOCUMENT)
	account:          WhatsApp Account name or number; defaults to the default/connected account
	idempotency_key:  calling twice with the same key returns the first message instead of sending twice
	"""
	if idempotency_key:
		existing = frappe.db.get_value("WhatsApp Message", {"idempotency_key": idempotency_key})
		if existing:
			return frappe.get_doc("WhatsApp Message", existing)

	if not message and not attachment:
		frappe.throw(_("Provide a message, an attachment, or both."))
	if reference_doctype and reference_name and not frappe.db.exists(reference_doctype, reference_name):
		frappe.throw(_("{0} {1} does not exist.").format(reference_doctype, reference_name))

	phone, chat_jid = _target(to, chat_jid)
	account_name = _resolve_account(account, allowed_accounts)
	device = frappe.db.get_value("WhatsApp Account", account_name, "device")

	doc = frappe.new_doc("WhatsApp Message")
	doc.update(
		{
			"direction": "Outgoing",
			"status": "Queued",
			"account": account_name,
			"device": device,
			"phone": phone,
			"chat_jid": chat_jid,
			"is_group": 1 if chat_jid.endswith("@g.us") else 0,
			"content": message,
			"command": "SEND_TEXT",
			"message_type": "text",
			"idempotency_key": idempotency_key or frappe.generate_hash(length=32),
			"reference_doctype": reference_doctype if reference_name else None,
			"reference_name": reference_name if reference_doctype else None,
			"attempts": 0,
		}
	)
	if attachment:
		file_doc, mimetype = _resolve_attachment(attachment)
		doc.command, doc.message_type = _command_for(mimetype, message)
		doc.media_file = file_doc.file_url
		doc.media_filename = file_doc.file_name
		doc.media_mimetype = mimetype
		doc.media_size = cint(file_doc.file_size)

	doc.insert(ignore_permissions=True)
	if attachment and not file_doc.attached_to_doctype:
		# A file uploaded just for this message belongs to it: it is then deleted
		# with the chat when the WhatsApp is unlinked or the user logs out. Files
		# attached to other documents (e.g. an invoice PDF) are left alone.
		frappe.db.set_value(
			"File",
			file_doc.name,
			{"attached_to_doctype": "WhatsApp Message", "attached_to_name": doc.name, "attached_to_field": "media_file"},
		)
	notify_device(device)
	return doc


def queue_reaction(target, emoji, idempotency_key=None):
	"""React to a WhatsApp Message with an emoji ("" removes this account's reaction)."""
	if idempotency_key:
		existing = frappe.db.get_value("WhatsApp Message", {"idempotency_key": idempotency_key})
		if existing:
			return frappe.get_doc("WhatsApp Message", existing)

	emoji = (emoji or "").strip()
	if len(emoji) > MAX_REACTION_LENGTH or re.search(r"[A-Za-z0-9<>]", emoji):
		frappe.throw(_("A reaction must be a single emoji."))

	msg = frappe.get_doc("WhatsApp Message", target)
	if msg.message_type == "reaction":
		frappe.throw(_("You cannot react to a reaction."))
	if not msg.whatsapp_message_id or not msg.account:
		frappe.throw(_("This message has not reached WhatsApp yet, so it cannot be reacted to."))
	if frappe.db.get_value("WhatsApp Account", msg.account, "status") == "logged_out":
		frappe.throw(_("This WhatsApp is no longer linked to a computer, so you cannot react from it."))

	chat_jid = msg.chat_jid or (f"{msg.phone}@s.whatsapp.net" if msg.phone else None)
	if not chat_jid:
		frappe.throw(_("The chat of this message is unknown."))
	from_me = msg.direction == "Outgoing"
	device = frappe.db.get_value("WhatsApp Account", msg.account, "device")

	doc = frappe.new_doc("WhatsApp Message")
	doc.update(
		{
			"direction": "Outgoing",
			"status": "Queued",
			"account": msg.account,
			"device": device,
			"phone": msg.phone,
			"chat_jid": chat_jid,
			"is_group": msg.is_group,
			"message_type": "reaction",
			"command": "SEND_REACTION",
			"content": emoji,
			"quoted_message_id": msg.whatsapp_message_id,
			# Key of the message being reacted to (needed by WhatsApp).
			"context_json": json.dumps(
				{
					"target": msg.name,
					"from_me": from_me,
					"participant": msg.sender_jid if (msg.is_group and not from_me) else None,
				}
			),
			"idempotency_key": idempotency_key or frappe.generate_hash(length=32),
			"reference_doctype": msg.reference_doctype,
			"reference_name": msg.reference_name,
			"attempts": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	notify_device(device)
	return doc


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def react(message, emoji):
	"""React to a message from the desk. Returns the reaction job name."""
	_require_sender()
	frappe.has_permission("WhatsApp Message", "read", message, throw=True)
	doc = queue_reaction(message, emoji)
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def send(to=None, message=None, attachment=None, account=None, reference_doctype=None, reference_name=None, idempotency_key=None, chat_jid=None):
	"""Queue a WhatsApp message. Returns the WhatsApp Message name and status."""
	_require_sender()
	if reference_doctype and reference_name:
		frappe.has_permission(reference_doctype, "read", reference_name, throw=True)
	if attachment:
		file_name = frappe.db.get_value("File", {"file_url": attachment}, "name") or attachment
		if frappe.db.exists("File", file_name):
			frappe.has_permission("File", "read", file_name, throw=True)
	from whatsapp_connector.access import allowed_accounts

	doc = queue_message(
		to, message, attachment, account, reference_doctype, reference_name, idempotency_key, chat_jid,
		allowed_accounts=allowed_accounts(),
	)
	return {"name": doc.name, "status": doc.status, "command": doc.command}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def cancel(name):
	"""Cancel a message that has not been handed to a device yet."""
	_require_sender()
	frappe.has_permission("WhatsApp Message", "read", name, throw=True)
	doc = frappe.get_doc("WhatsApp Message", name)
	if doc.direction != "Outgoing" or doc.status != "Queued":
		frappe.throw(_("Only queued outgoing messages can be cancelled."))
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def retry(name):
	"""Put a failed or cancelled message back in the queue."""
	_require_sender()
	frappe.has_permission("WhatsApp Message", "read", name, throw=True)
	doc = frappe.get_doc("WhatsApp Message", name)
	if doc.direction != "Outgoing" or doc.status not in ("Failed", "Cancelled"):
		frappe.throw(_("Only failed or cancelled outgoing messages can be retried."))
	doc.update({"status": "Queued", "attempts": 0, "error": None, "lease_expires_at": None})
	doc.save(ignore_permissions=True)
	notify_device(doc.device or frappe.db.get_value("WhatsApp Account", doc.account, "device"))
	return {"name": doc.name, "status": doc.status}
