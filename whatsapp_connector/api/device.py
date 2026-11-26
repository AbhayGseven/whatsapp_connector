"""Device-facing API used by the WhatsApp ERP Connector desktop application.

Authentication: every call uses the API key/secret of an ERPNext user that has
the "WhatsApp Device" role (header ``Authorization: token <key>:<secret>``).
Each computer additionally identifies itself with its device UUID; a device is
bound to the user that registered it and must be approved by a WhatsApp
Manager before it can exchange messages.

All endpoints are idempotent so the desktop app can retry safely.
"""

import base64
import binascii
import json
import re

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from whatsapp_connector.access import publish_chat_event
from whatsapp_connector.utils import (
	retry_on_conflict,
	EVENT_DEVICE_STATUS,
	EVENT_JOBS_AVAILABLE,
	ROLE_DEVICE,
	epoch_to_datetime,
	get_settings,
	normalize_phone,
	parse_json_arg,
	to_bool,
	validate_uuid,
)

MAX_INCOMING_BATCH = 100
MAX_RECEIPTS_BATCH = 200
# One WhatsApp number per computer.
MAX_ACCOUNTS = 1
MAX_CONTEXT_CHARS = 10_000
MESSAGE_TYPES = {"text", "image", "document", "video", "audio", "sticker", "location", "contact", "reaction", "other"}
ACCOUNT_STATUSES = {"connected", "connecting", "qr", "reconnecting", "disconnected", "logged_out", "error"}
RECEIPT_RANK = {"Sent": 1, "Delivered": 2, "Read": 3}


class ConnectorDisabled(frappe.ValidationError):
	http_status_code = 503


class DeviceNotRegistered(frappe.ValidationError):
	http_status_code = 404


class DeviceNotApproved(frappe.PermissionError):
	http_status_code = 403


# ---------------------------------------------------------------- helpers


def _require_device_user():
	if frappe.session.user == "Guest":
		raise frappe.AuthenticationError
	if ROLE_DEVICE not in frappe.get_roles():
		frappe.throw(
			_("User {0} needs the {1} role to act as a WhatsApp device.").format(frappe.session.user, ROLE_DEVICE),
			frappe.PermissionError,
		)
	if not get_settings().enabled:
		frappe.throw(_("WhatsApp Connector is disabled on this ERPNext site."), ConnectorDisabled)


def _check_device_identity(uuid, owner):
	"""Token-authenticated requests may only act as the device the token belongs
	to (all enrolled devices share the service user, so the user alone is not
	enough). API-key requests may only act on devices registered by that user."""
	token_device = getattr(frappe.local, "whatsapp_device", None)
	if token_device:
		if token_device != uuid:
			frappe.throw(_("This device token belongs to a different device."), frappe.PermissionError)
	elif owner != frappe.session.user:
		frappe.throw(_("Device {0} belongs to another user.").format(uuid), frappe.PermissionError)


def _get_device(device_uuid, require_approved=True):
	_require_device_user()
	uuid = validate_uuid(device_uuid, "device UUID")
	device = frappe.db.get_value("WhatsApp Device", uuid, ["name", "user", "status"], as_dict=True)
	if not device:
		frappe.throw(_("Device {0} is not registered.").format(uuid), DeviceNotRegistered)
	_check_device_identity(uuid, device.user)
	if require_approved and device.status != "Approved":
		frappe.throw(_("Device {0} is {1}.").format(uuid, device.status), DeviceNotApproved)
	return device


def _account_devices(device_name):
	return set(frappe.get_all("WhatsApp Account", filters={"device": device_name}, pluck="name"))


def _client_config():
	s = get_settings()
	return {
		"heartbeat_interval": s.heartbeat_interval,
		"poll_interval": s.poll_interval,
		"job_lease_seconds": s.job_lease_seconds,
		"max_media_size_mb": s.max_media_size_mb,
		# Socket.IO namespace is the site name; URL defaults to the site's own origin.
		"history_sync_days": s.history_sync_days,
		"media_download": s.media_download or "On Request",
		"history_include_media": bool(s.history_include_media),
		"site_name": frappe.local.site,
		"realtime_url": (s.realtime_url or "").strip() or None,
		"realtime_events": {"jobs": EVENT_JOBS_AVAILABLE, "device_status": EVENT_DEVICE_STATUS},
	}


def _pending_job_count(device_name):
	return frappe.db.sql(
		"""select count(*) from `tabWhatsApp Message` m
		inner join `tabWhatsApp Account` a on a.name = m.account
		where m.direction = 'Outgoing' and m.status = 'Queued' and a.device = %s""",
		device_name,
	)[0][0]


def _clip(value, length):
	return str(value)[:length] if value not in (None, "") else None


def _job_owned_by(device_name, job_id):
	job = frappe.db.get_value(
		"WhatsApp Message",
		job_id,
		["name", "direction", "status", "account", "attempts", "media_file", "media_filename"],
		as_dict=True,
	)
	if not job or job.direction != "Outgoing":
		frappe.throw(_("Job {0} not found.").format(job_id), frappe.DoesNotExistError)
	if frappe.db.get_value("WhatsApp Account", job.account, "device") != device_name:
		frappe.throw(_("Job {0} is not assigned to this device.").format(job_id), frappe.PermissionError)
	return job


# ---------------------------------------------------------------- registration & heartbeat


@frappe.whitelist(methods=["POST"])
def register_device(device_uuid, device_name=None, hostname=None, platform=None, app_version=None):
	"""Register this computer (or refresh its details). New devices wait for approval
	unless WhatsApp Connector Settings > Auto-approve New Devices is on."""
	_require_device_user()
	uuid = validate_uuid(device_uuid, "device UUID")
	owner = frappe.db.get_value("WhatsApp Device", uuid, "user")
	if owner:
		_check_device_identity(uuid, owner)
	elif getattr(frappe.local, "whatsapp_device", None):
		frappe.throw(_("This device token belongs to a different device."), frappe.PermissionError)

	if owner:
		doc = frappe.get_doc("WhatsApp Device", uuid)
	else:
		doc = frappe.new_doc("WhatsApp Device")
		doc.device_uuid = uuid
		doc.user = frappe.session.user
		doc.registered_on = now_datetime()
		doc.status = "Approved" if get_settings().auto_approve_devices else "Pending Approval"
		doc.auth_method = "API Key"

	doc.device_name = _clip(device_name, 140) or doc.device_name or _clip(hostname, 140) or uuid[:8]
	doc.hostname = _clip(hostname, 140)
	doc.platform = _clip(platform, 140)
	doc.app_version = _clip(app_version, 140)
	doc.last_ip = frappe.local.request_ip
	doc.save(ignore_permissions=True) if owner else doc.insert(ignore_permissions=True)

	return {
		"device_uuid": doc.name,
		"device_name": doc.device_name,
		"status": doc.status,
		"config": _client_config(),
		"server_time": now_datetime(),
	}


def _merge_if_relinked(account_id):
	"""Same user linked the same number again: continue the earlier chats here."""
	acc = frappe.db.get_value("WhatsApp Account", account_id, ["user", "phone_number"], as_dict=True)
	if acc and acc.user and acc.phone_number and frappe.db.exists(
		"WhatsApp Account", {"user": acc.user, "phone_number": acc.phone_number, "name": ("!=", account_id)}
	):
		from whatsapp_connector.accounts import request_merge

		request_merge(account_id)


def _sync_accounts(device_name, accounts):
	if not isinstance(accounts, list):
		frappe.throw(_("accounts must be a list."))
	now = now_datetime()
	for item in accounts[:MAX_ACCOUNTS]:
		if not isinstance(item, dict):
			continue
		account_id = validate_uuid(item.get("account_id"), "account ID")
		status = item.get("status") if item.get("status") in ACCOUNT_STATUSES else "error"
		phone = item.get("phone_number")
		values = {
			"label": _clip(item.get("label"), 140),
			"phone_number": normalize_phone(phone) if phone else None,
			"jid": _clip(item.get("jid"), 140),
			"status": status,
			"last_status_at": now,
		}
		current = frappe.db.get_value("WhatsApp Account", account_id, ["device", "phone_number"], as_dict=True)
		owner = current.device if current else None
		if owner and owner != device_name:
			continue  # an account ID can never move between computers
		if owner:
			if not values["phone_number"]:
				values.pop("phone_number")  # keep the known number while reconnecting
			frappe.db.set_value("WhatsApp Account", account_id, values)
			if current.phone_number and values.get("phone_number") and values["phone_number"] != current.phone_number:
				# A different number was linked under this account record: keep the
				# earlier number's chats separate (its own account, no longer linked).
				from whatsapp_connector.accounts import request_split

				request_split(account_id, current.phone_number, now)
			if values.get("phone_number") and values["phone_number"] != current.phone_number:
				from whatsapp_connector.whatsapp_connector.doctype.whatsapp_message.whatsapp_message import (
					fill_message_numbers,
				)

				fill_message_numbers(account_id)  # messages stored before the number was known
		elif not values["phone_number"] and status in ("logged_out", "error", "disconnected"):
			continue  # a QR code that ended unscanned: nothing to keep
		else:
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_id": account_id,
					"device": device_name,
					# Owner for good: the chats stay with this user after logout / unlinking.
					"user": frappe.db.get_value("WhatsApp Device", device_name, "assigned_user"),
					**values,
				}
			).insert(ignore_permissions=True)
		_merge_if_relinked(account_id)


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def heartbeat(device_uuid, whatsapp_status=None, accounts=None, app_version=None, last_error=None, going_offline=0):
	"""Called every `heartbeat_interval` seconds. Keeps the device Online, syncs the
	WhatsApp account list/statuses and returns how many jobs are waiting.
	going_offline=1: the app is quitting (or the computer shutting down) - show it
	as disconnected now instead of waiting for the offline timeout."""
	device = _get_device(device_uuid, require_approved=False)
	if to_bool(going_offline):
		from whatsapp_connector.api.login import mark_connection_offline

		frappe.db.set_value("WhatsApp Device", device.name, "connection_status", "Offline", update_modified=False)
		frappe.db.set_value(
			"WhatsApp Account",
			{"device": device.name, "status": ("!=", "logged_out")},
			"status",
			"disconnected",
			update_modified=False,
		)
		mark_connection_offline(device.name)
		return {"status": device.status, "offline": True}
	values = {
		"last_heartbeat": now_datetime(),
		"connection_status": "Online",
		"whatsapp_status": _clip(whatsapp_status, 140),
		"last_ip": frappe.local.request_ip,
		"last_error": _clip(last_error, 1000),
	}
	if app_version:
		values["app_version"] = _clip(app_version, 140)
	frappe.db.set_value("WhatsApp Device", device.name, values, update_modified=False)

	approved = device.status == "Approved"
	if approved and accounts is not None:
		items = parse_json_arg(accounts, [])
		_sync_accounts(device.name, items)
		from whatsapp_connector.api.login import update_connection_whatsapp

		first = items[0] if items and isinstance(items[0], dict) else {}
		phone = first.get("phone_number")
		try:
			phone = normalize_phone(phone) if phone else None
		except frappe.ValidationError:
			frappe.clear_messages()
			phone = None
		update_connection_whatsapp(device.name, phone, first.get("status") if first else None)

	return {
		"status": device.status,
		"pending_jobs": _pending_job_count(device.name) if approved else 0,
		"config": _client_config(),
		"server_time": now_datetime(),
	}


# ---------------------------------------------------------------- incoming messages


def _phone_or_none(raw):
	"""Senders identified only by a WhatsApp LID (privacy ID) have no phone number;
	keep the message and leave phone empty instead of rejecting it."""
	if not raw:
		return None
	try:
		return normalize_phone(raw, country_code="")
	except frappe.ValidationError:
		frappe.clear_messages()
		return None


MAX_THUMBNAIL_CHARS = 16_000
THUMBNAIL_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def _context_json(context, thumbnail=None):
	"""Message context as JSON (or None). A small base64 JPEG preview of the media
	(WhatsApp's own thumbnail) is kept for media that is downloaded on request."""
	context = dict(context) if isinstance(context, dict) else ({"value": context} if context else {})
	if isinstance(thumbnail, str) and len(thumbnail) <= MAX_THUMBNAIL_CHARS and THUMBNAIL_RE.match(thumbnail):
		context["thumbnail"] = thumbnail
	if not context:
		return None
	text = json.dumps(context)
	if len(text) > MAX_CONTEXT_CHARS + MAX_THUMBNAIL_CHARS:
		context.pop("thumbnail", None)
		text = json.dumps(context)
		if len(text) > MAX_CONTEXT_CHARS:
			text = json.dumps({"truncated": True})
	return text


def _incoming_doc(device_name, item):
	account_id = validate_uuid(item.get("account_id"), "account ID")
	message_id = _clip(item.get("message_id"), 100)
	if not message_id:
		frappe.throw(_("message_id is required."))

	from_me = to_bool(item.get("from_me"))
	# Only a phone-number JID carries a phone; the digits of a privacy ID (@lid)
	# or group (@g.us) are not a phone number.
	chat_jid = item.get("chat_jid") or ""
	sender_phone = item.get("sender_phone") or (
		chat_jid.split("@")[0].split(":")[0] if chat_jid.endswith("@s.whatsapp.net") else None
	)
	media = item.get("media") if isinstance(item.get("media"), dict) else {}
	context = _context_json(item.get("context"), media.get("thumbnail"))
	message_type = item.get("message_type") if item.get("message_type") in MESSAGE_TYPES else "other"

	return frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			# Messages typed on the phone itself are recorded as Outgoing/Sent history.
			"direction": "Outgoing" if from_me else "Incoming",
			"status": "Sent" if from_me else "Received",
			"device": device_name,
			"account": account_id,
			"dedupe_key": f"{device_name}:{account_id}:{message_id}",
			"whatsapp_message_id": message_id,
			"phone": _phone_or_none(sender_phone),
			"chat_jid": _clip(item.get("chat_jid"), 140),
			"sender_jid": _clip(item.get("sender_jid"), 140),
			"sender_name": _clip(item.get("sender_name"), 140),
			"is_group": 1 if to_bool(item.get("is_group")) else 0,
			"message_type": message_type,
			"content": item.get("content"),
			"message_timestamp": epoch_to_datetime(item.get("timestamp")),
			"quoted_message_id": _clip(item.get("quoted_message_id"), 100),
			"media_filename": _clip(media.get("filename"), 140),
			"media_mimetype": _clip(media.get("mimetype"), 140),
			"media_size": cint(media.get("size")) or None,
			"sent_at": epoch_to_datetime(item.get("timestamp")) if from_me else None,
			"context_json": context,
			# Imported chat history is not "new": do not count it as unread.
			"is_read": 1 if to_bool(item.get("history")) else 0,
		}
	)


def _existing_by_message_id(pairs):
	"""{(account, whatsapp_message_id): name} for messages already stored."""
	pairs = [(a, m) for a, m in pairs if a and m]
	if not pairs:
		return {}
	rows = frappe.db.sql(
		"""select account, whatsapp_message_id, name from `tabWhatsApp Message`
		where account in %(accounts)s and whatsapp_message_id in %(ids)s""",
		{"accounts": tuple({a for a, _m in pairs}), "ids": tuple({m for _a, m in pairs})},
	)
	return {(a, m): n for a, m, n in rows}


def _bulk_insert_history(device_name, items, result):
	"""Insert history messages with a single INSERT. Returns the ids (id(item)) of
	the items it handled (accepted or duplicate); an empty set means "not handled,
	use the per-message path"."""
	from whatsapp_connector.whatsapp_connector.doctype.whatsapp_message.whatsapp_message import (
		compute_conversation_key,
	)

	now = now_datetime()
	user = frappe.session.user
	key_cache = {}
	rows, pending, lid_phones = [], [], set()
	frappe.db.savepoint("wa_history")
	try:
		for item in items:
			doc = _incoming_doc(device_name, item)
			cache_key = (doc.account, doc.chat_jid, doc.phone, doc.is_group)
			if cache_key not in key_cache:
				key_cache[cache_key] = compute_conversation_key(*cache_key)
			doc.conversation_key = key_cache[cache_key]
			doc.name = frappe.generate_hash(length=10)
			doc.owner = doc.modified_by = user
			doc.creation = doc.modified = now
			doc.docstatus = 0
			doc._set_defaults()
			doc.validate()
			rows.append(doc.get_valid_dict(convert_dates_to_str=True, ignore_nulls=False))
			pending.append((item, doc))
			if doc.phone and not doc.is_group and (doc.chat_jid or "").endswith("@lid"):
				lid_phones.add((doc.phone, doc.account, doc.chat_jid))

		existing = dict(
			frappe.db.sql(
				"select dedupe_key, name from `tabWhatsApp Message` where dedupe_key in %s",
				(tuple(d.dedupe_key for _i, d in pending),),
			)
		)
		# Also a duplicate: the same WhatsApp message already stored for this account by
		# another path (sent from ERPNext, merged from an earlier link of the number).
		same_id = _existing_by_message_id([(d.account, d.whatsapp_message_id) for _i, d in pending])
		new_rows = [
			r for r in rows
			if r["dedupe_key"] not in existing and (r["account"], r["whatsapp_message_id"]) not in same_id
		]
		seen = set()
		new_rows = [r for r in new_rows if not (r["dedupe_key"] in seen or seen.add(r["dedupe_key"]))]
		if new_rows:
			fields = list(new_rows[0].keys())
			frappe.db.bulk_insert(
				"WhatsApp Message", fields, [[r[f] for f in fields] for r in new_rows], ignore_duplicates=True
			)
		# Same effect as WhatsAppMessage.after_insert, once per chat.
		for phone, account, chat_jid in lid_phones:
			frappe.db.sql(
				"""update `tabWhatsApp Message` set conversation_key = %s, phone = ifnull(phone, %s)
				where account = %s and chat_jid = %s and conversation_key = %s""",
				(phone, phone, account, chat_jid, chat_jid),
			)
		if lid_phones:
			from whatsapp_connector.whatsapp_connector.doctype.whatsapp_message.whatsapp_message import (
				fill_message_numbers,
			)

			for account in {a for _p, a, _c in lid_phones}:
				fill_message_numbers(account)
		stored = dict(
			frappe.db.sql(
				"select dedupe_key, name from `tabWhatsApp Message` where dedupe_key in %s",
				(tuple(d.dedupe_key for _i, d in pending),),
			)
		)
	except frappe.QueryDeadlockError:
		raise  # conflicting concurrent request: the whole call is retried
	except Exception:
		frappe.db.rollback(save_point="wa_history")
		frappe.clear_messages()
		return set()

	for item, doc in pending:
		name = stored.get(doc.dedupe_key) or same_id.get((doc.account, doc.whatsapp_message_id))
		entry = {"message_id": str(item.get("message_id") or ""), "name": name}
		if name == doc.name:
			result["accepted"].append(entry)
		else:
			result["duplicates"].append(entry)
	return {id(item) for item, _doc in pending}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def push_incoming(device_uuid, messages):
	"""Store a batch of received WhatsApp messages. Duplicates (same device, account
	and WhatsApp message ID) are acknowledged without creating a second record, so
	the desktop queue can retry freely. Each item is acknowledged individually."""
	device = _get_device(device_uuid)
	items = parse_json_arg(messages, [])
	if not isinstance(items, list) or len(items) > MAX_INCOMING_BATCH:
		frappe.throw(_("messages must be a list of at most {0} items.").format(MAX_INCOMING_BATCH))

	own_accounts = _account_devices(device.name)
	result = {"accepted": [], "duplicates": [], "rejected": []}
	history_accounts = set()

	# Chat history arrives in bulk: one duplicate lookup and one multi-row INSERT
	# per batch instead of a full document save per message. Items the fast path
	# cannot take (or any failure in it) fall through to the per-message path.
	history_items = [
		i for i in items if isinstance(i, dict) and to_bool(i.get("history")) and i.get("account_id") in own_accounts
	]
	done = _bulk_insert_history(device.name, history_items, result) if history_items else set()
	if done:
		history_accounts.update(i["account_id"] for i in history_items if id(i) in done)

	for item in items:
		if id(item) in done:
			continue
		message_id = str((item or {}).get("message_id") or "") if isinstance(item, dict) else ""
		frappe.db.savepoint("wa_incoming")
		try:
			if not isinstance(item, dict):
				frappe.throw(_("Each message must be an object."))
			if item.get("account_id") not in own_accounts:
				frappe.throw(_("Unknown account {0} for this device. Send a heartbeat first.").format(item.get("account_id")))

			dedupe_key = f"{device.name}:{item['account_id']}:{message_id}"
			existing = frappe.db.get_value("WhatsApp Message", {"dedupe_key": dedupe_key}) or (
				message_id
				and frappe.db.get_value(
					"WhatsApp Message", {"account": item["account_id"], "whatsapp_message_id": message_id[:100]}
				)
			)
			if existing:
				result["duplicates"].append({"message_id": message_id, "name": existing})
				continue

			doc = _incoming_doc(device.name, item)
			if to_bool(item.get("history")):
				doc.flags.skip_chat_update = True  # bulk import: one refresh per batch below
				history_accounts.add(doc.account)
			doc.insert(ignore_permissions=True)
			result["accepted"].append({"message_id": message_id, "name": doc.name})
		except frappe.DuplicateEntryError:
			frappe.db.rollback(save_point="wa_incoming")
			existing = frappe.db.get_value("WhatsApp Message", {"dedupe_key": f"{device.name}:{item.get('account_id')}:{message_id}"})
			result["duplicates"].append({"message_id": message_id, "name": existing})
		except frappe.QueryDeadlockError:
			raise  # conflicting concurrent request: the whole call is retried
		except Exception as e:
			frappe.db.rollback(save_point="wa_incoming")
			frappe.clear_messages()
			result["rejected"].append({"message_id": message_id, "error": str(e)[:300] or e.__class__.__name__})

	for account in history_accounts:
		publish_chat_event(account, {"account": account, "bulk": True}, after_commit=True)
	return result


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def upload_incoming_media(device_uuid, account_id, message_id, filename, content, mimetype=None):
	"""Attach the media of an already pushed incoming message (base64 content).
	Stored as a private File on the WhatsApp Message. Idempotent."""
	device = _get_device(device_uuid)
	account_id = validate_uuid(account_id, "account ID")
	name = frappe.db.get_value("WhatsApp Message", {"dedupe_key": f"{device.name}:{account_id}:{message_id}"})
	if not name:
		frappe.throw(_("Push the message before uploading its media."), frappe.DoesNotExistError)

	doc = frappe.get_doc("WhatsApp Message", name)
	if doc.media_file:
		return {"name": doc.name, "file_url": doc.media_file, "duplicate": True}

	try:
		data = base64.b64decode(content or "", validate=True)
	except (binascii.Error, ValueError):
		frappe.throw(_("Media content must be base64 encoded."))
	if not data:
		frappe.throw(_("Media content is empty."))
	if len(data) > get_settings().max_media_size_mb * 1024 * 1024:
		frappe.throw(_("Media is larger than the configured limit."))

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": _clip(filename, 140) or f"{message_id}.bin",
			"attached_to_doctype": "WhatsApp Message",
			"attached_to_name": doc.name,
			"attached_to_field": "media_file",
			"is_private": 1,
			"content": data,
		}
	).insert(ignore_permissions=True)

	doc.media_file = file_doc.file_url
	_set_media_request(doc, None)
	doc.media_filename = doc.media_filename or file_doc.file_name
	doc.media_mimetype = doc.media_mimetype or _clip(mimetype, 140)
	doc.media_size = len(data)
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "file_url": doc.media_file, "duplicate": False}


def _context(doc):
	try:
		return json.loads(doc.context_json or "{}") or {}
	except ValueError:
		return {}


def _set_media_request(doc, state, error=None):
	"""state: "pending" (user clicked Download), "failed", or None (done/cleared)."""
	context = _context(doc)
	context.pop("media_error", None)
	if state:
		context["media_request"] = state
	else:
		context.pop("media_request", None)
	if error:
		context["media_error"] = str(error)[:300]
	doc.context_json = json.dumps(context) if context else None


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def claim_media_requests(device_uuid, limit=10):
	"""Media files users asked to download (WhatsApp Chat > Download) for this
	computer's WhatsApp. The app fetches each from WhatsApp and calls
	upload_incoming_media, or report_media_failed."""
	device = _get_device(device_uuid)
	accounts = list(_account_devices(device.name))
	if not accounts:
		return {"requests": []}
	rows = frappe.db.sql(
		"""select name, account, whatsapp_message_id, message_type, media_filename, media_mimetype
		from `tabWhatsApp Message`
		where account in %(accounts)s and (media_file is null or media_file = '')
		  and context_json like %(pending)s
		order by modified limit %(limit)s""",
		{"accounts": tuple(accounts), "pending": '%"media_request": "pending"%', "limit": min(max(cint(limit) or 10, 1), 50)},
		as_dict=True,
	)
	return {
		"requests": [
			{
				"account_id": r.account,
				"message_id": r.whatsapp_message_id,
				"message_type": r.message_type,
				"filename": r.media_filename,
				"mimetype": r.media_mimetype,
			}
			for r in rows
		]
	}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def report_media_failed(device_uuid, account_id, message_id, error=None):
	"""The app could not fetch a requested file (expired on WhatsApp and the phone
	no longer has it, too large, not available on this computer...)."""
	device = _get_device(device_uuid)
	account_id = validate_uuid(account_id, "account ID")
	name = frappe.db.get_value("WhatsApp Message", {"dedupe_key": f"{device.name}:{account_id}:{message_id}"})
	if not name:
		return {"ok": False}
	doc = frappe.get_doc("WhatsApp Message", name)
	if doc.media_file:
		return {"ok": True}
	_set_media_request(doc, "failed", error or _("The file could not be downloaded."))
	doc.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def purge_account(device_uuid, account_id, remove_account=0):
	"""The WhatsApp was logged out from the phone or unlinked on this computer.
	Its chats and contacts are KEPT in ERPNext (with their owner); the account is
	only marked as no longer linked. (Name kept for older desktop apps.)"""
	device = _get_device(device_uuid, require_approved=False)
	account_id = validate_uuid(account_id, "account ID")
	owner = frappe.db.get_value("WhatsApp Account", account_id, "device")
	if owner and owner != device.name:
		frappe.throw(_("Account {0} belongs to another device.").format(account_id), frappe.PermissionError)
	if owner and _is_empty_never_linked(account_id):
		# A QR code that was never scanned: nothing to keep.
		frappe.db.delete("WhatsApp Account", {"name": account_id})
		return {"queued": False, "kept": False}
	if owner:
		frappe.db.set_value("WhatsApp Account", account_id, {"status": "logged_out", "is_default": 0})
	return {"queued": False, "kept": bool(owner)}


def _is_empty_never_linked(account_id):
	return not frappe.db.get_value("WhatsApp Account", account_id, "phone_number") and not (
		frappe.db.exists("WhatsApp Message", {"account": account_id})
		or frappe.db.exists("WhatsApp Contact", {"account": account_id})
	)


MAX_CONTACTS_BATCH = 200


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def push_contacts(device_uuid, contacts):
	"""Upsert WhatsApp contacts / groups / privacy-ID mappings:
	[{account_id, jid, lid, phone, name, notify, verified_name}] (max 500)."""
	from whatsapp_connector.contacts import upsert_contact

	device = _get_device(device_uuid)
	items = parse_json_arg(contacts, [])
	if not isinstance(items, list) or len(items) > MAX_CONTACTS_BATCH:
		frappe.throw(_("contacts must be a list of at most {0} items.").format(MAX_CONTACTS_BATCH))
	own_accounts = _account_devices(device.name)
	upserted, skipped = 0, 0
	for item in items:
		if not isinstance(item, dict) or item.get("account_id") not in own_accounts:
			skipped += 1
			continue
		frappe.db.savepoint("wa_contact")
		try:
			if upsert_contact(item["account_id"], item):
				upserted += 1
			else:
				skipped += 1
		except frappe.QueryDeadlockError:
			raise  # conflicting concurrent request: the whole call is retried
		except Exception:
			frappe.db.rollback(save_point="wa_contact")
			frappe.clear_messages()
			skipped += 1
	return {"upserted": upserted, "skipped": skipped}


# ---------------------------------------------------------------- outgoing jobs


# Desktop apps before this version only know image/document/PDF media commands.
NATIVE_AV_MIN_VERSION = (0, 8, 1)


def _supports_native_av(device_name):
	version = frappe.db.get_value("WhatsApp Device", device_name, "app_version") or ""
	try:
		return tuple(int(p) for p in version.split(".")[:3]) >= NATIVE_AV_MIN_VERSION
	except ValueError:
		return False


def _job_payload(device_name, job):
	command = job.command
	if command in ("SEND_VIDEO", "SEND_AUDIO") and not _supports_native_av(device_name):
		command = "SEND_DOCUMENT"  # still delivered, as a file
	payload = {
		"job_id": job.name,
		"command": command,
		"account_id": job.account,
		"to": job.phone,
		"chat_jid": job.chat_jid or (f"{job.phone}@s.whatsapp.net" if job.phone else None),
		"content": job.content,
		"idempotency_key": job.idempotency_key,
		"attempt": job.attempts,
		"lease_expires_at": job.lease_expires_at,
		"media": None,
	}
	if job.command == "SEND_REACTION":
		key = json.loads(job.context_json or "{}")
		payload["reaction"] = {
			"message_id": job.quoted_message_id,
			"from_me": bool(key.get("from_me")),
			"participant": key.get("participant"),
		}
	if job.media_file:
		payload["media"] = {
			"url": "/api/method/whatsapp_connector.api.device.download_job_media"
			f"?device_uuid={device_name}&job_id={job.name}",
			"filename": job.media_filename,
			"mimetype": job.media_mimetype,
			"size": job.media_size,
		}
	return payload


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def claim_jobs(device_uuid, limit=10):
	"""Lease up to `limit` queued outgoing messages for this device's accounts.
	A leased job returns to the queue if no result is reported before the lease
	expires. Jobs stay in ERPNext while the device is offline."""
	device = _get_device(device_uuid)
	limit = min(max(cint(limit) or 10, 1), 50)
	settings = get_settings()
	now = now_datetime()
	lease_until = add_to_date(now, seconds=settings.job_lease_seconds)

	names = frappe.db.sql(
		"""select m.name from `tabWhatsApp Message` m
		inner join `tabWhatsApp Account` a on a.name = m.account
		where m.direction = 'Outgoing' and a.device = %(device)s
		  and (m.status = 'Queued'
		       or (m.status = 'Sending' and m.lease_expires_at < %(now)s))
		order by m.creation
		limit %(limit)s
		for update skip locked""",
		{"device": device.name, "now": now, "limit": limit},
		pluck=True,
	)

	jobs = []
	for name in names:
		job = frappe.get_doc("WhatsApp Message", name)
		job.attempts = (job.attempts or 0) + 1
		if job.attempts > settings.max_send_attempts:
			job.status = "Failed"
			job.error = _("Gave up after {0} attempts.").format(settings.max_send_attempts)
			job.lease_expires_at = None
			job.save(ignore_permissions=True)
			continue
		job.status = "Sending"
		job.device = device.name
		job.lease_expires_at = lease_until
		job.save(ignore_permissions=True)
		jobs.append(_job_payload(device.name, job))

	return {"jobs": jobs, "server_time": now}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def report_job_result(device_uuid, job_id, success, whatsapp_message_id=None, error=None, retryable=0, sent_at=None):
	"""Report the outcome of a claimed job. `success` means WhatsApp accepted the
	message (status Sent); delivery and read arrive later via report_receipts.
	Reporting the same result twice is harmless."""
	device = _get_device(device_uuid)
	job_info = _job_owned_by(device.name, job_id)
	doc = frappe.get_doc("WhatsApp Message", job_info.name)

	if doc.status in RECEIPT_RANK:
		return {"job_id": doc.name, "status": doc.status, "duplicate": True}

	if to_bool(success):
		doc.status = "Sent"
		doc.whatsapp_message_id = _clip(whatsapp_message_id, 100)
		doc.sent_at = epoch_to_datetime(sent_at) or now_datetime()
		doc.error = None
	else:
		doc.error = _clip(error, 1000) or _("Unknown error")
		max_attempts = get_settings().max_send_attempts
		if doc.status == "Cancelled":
			pass
		elif to_bool(retryable) and (doc.attempts or 0) < max_attempts:
			doc.status = "Queued"
		else:
			doc.status = "Failed"
	doc.lease_expires_at = None
	doc.save(ignore_permissions=True)
	return {"job_id": doc.name, "status": doc.status, "duplicate": False}


@frappe.whitelist(methods=["POST"])
@retry_on_conflict
def report_receipts(device_uuid, receipts):
	"""Delivery/read receipts for sent messages: [{account_id, message_id,
	status: delivered|read|played, timestamp}]. Status only moves forward."""
	device = _get_device(device_uuid)
	items = parse_json_arg(receipts, [])
	if not isinstance(items, list) or len(items) > MAX_RECEIPTS_BATCH:
		frappe.throw(_("receipts must be a list of at most {0} items.").format(MAX_RECEIPTS_BATCH))

	own_accounts = _account_devices(device.name)
	mapping = {"delivered": "Delivered", "read": "Read", "played": "Read"}
	updated, unknown = 0, []

	for item in items:
		if not isinstance(item, dict) or item.get("account_id") not in own_accounts:
			continue
		new_status = mapping.get(str(item.get("status", "")).lower())
		name = frappe.db.get_value(
			"WhatsApp Message",
			{
				"account": item["account_id"],
				"whatsapp_message_id": item.get("message_id"),
				"direction": "Outgoing",
			},
		)
		if not name or not new_status:
			unknown.append(item.get("message_id"))
			continue
		doc = frappe.get_doc("WhatsApp Message", name)
		if RECEIPT_RANK.get(new_status, 0) <= RECEIPT_RANK.get(doc.status, 0):
			continue
		at = epoch_to_datetime(item.get("timestamp")) or now_datetime()
		doc.status = new_status
		doc.delivered_at = doc.delivered_at or at
		if new_status == "Read":
			doc.read_at = at
		doc.save(ignore_permissions=True)
		updated += 1

	return {"updated": updated, "unknown": unknown}


@frappe.whitelist(methods=["GET"])
def download_job_media(device_uuid, job_id):
	"""Stream the attachment of a job currently leased by this device."""
	device = _get_device(device_uuid)
	job = _job_owned_by(device.name, job_id)
	if job.status != "Sending":
		frappe.throw(_("Job {0} is not currently claimed.").format(job_id), frappe.PermissionError)
	if not job.media_file:
		frappe.throw(_("Job {0} has no media.").format(job_id), frappe.DoesNotExistError)

	file_name = frappe.db.get_value("File", {"file_url": job.media_file}, "name")
	if not file_name:
		frappe.throw(_("The attachment for job {0} no longer exists.").format(job_id), frappe.DoesNotExistError)
	file_doc = frappe.get_doc("File", file_name)

	frappe.local.response.filename = job.media_filename or file_doc.file_name
	# encodings=[] -> raw bytes. By default File.get_content() decodes anything that
	# happens to be valid text in a fallback encoding (e.g. Windows-1252), which
	# corrupts binary files such as PNG/JPEG/PDF on the way out.
	frappe.local.response.filecontent = file_doc.get_content(encodings=[])
	frappe.local.response.type = "download"
