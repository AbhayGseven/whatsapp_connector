"""WhatsApp accounts are kept after logout / unlinking, with their chats.

Each account belongs to the ERPNext user who linked it (WhatsApp Account.user).
When that user links the same number again (a new account record on the
computer), the earlier account's chats are merged into the new one, so the chat
history continues in one place and re-imported history is not duplicated.
"""

import uuid

import frappe

CHUNK = 2000


def request_merge(account):
	frappe.enqueue(
		"whatsapp_connector.accounts.merge_previous_accounts",
		queue="long",
		timeout=3600,
		job_id=f"whatsapp_merge::{account}",
		deduplicate=True,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		account=account,
	)


def _commit():
	if not frappe.flags.in_test:
		frappe.db.commit()


def merge_previous_accounts(account):
	new = frappe.db.get_value("WhatsApp Account", account, ["name", "user", "phone_number", "device"], as_dict=True)
	if not new or not new.user or not new.phone_number:
		return
	old_accounts = frappe.get_all(
		"WhatsApp Account",
		filters={"user": new.user, "phone_number": new.phone_number, "name": ("!=", new.name)},
		pluck="name",
	)
	for old in old_accounts:
		_merge(old, new)
	if old_accounts:
		frappe.publish_realtime("whatsapp_chat_update", {"account": new.name, "bulk": True}, user=new.user)


def _merge(old, new):
	# 1. Messages imported again after re-linking: the earlier copy is kept (it has
	#    the read status and any downloaded file), the new duplicate is removed.
	while True:
		dupes = frappe.db.sql(
			"""select n.name from `tabWhatsApp Message` n
			inner join `tabWhatsApp Message` o
			  on o.account = %(old)s and o.whatsapp_message_id = n.whatsapp_message_id
			where n.account = %(new)s and n.whatsapp_message_id is not null
			limit %(chunk)s""",
			{"old": old, "new": new.name, "chunk": CHUNK},
			pluck=True,
		)
		if not dupes:
			break
		for f in frappe.get_all(
			"File", filters={"attached_to_doctype": "WhatsApp Message", "attached_to_name": ("in", dupes)}, pluck="name"
		):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.delete("WhatsApp Message", {"name": ("in", dupes)})
		_commit()

	# 2. Move the earlier messages to the new account (keys follow the new computer).
	while True:
		names = frappe.db.sql(
			"select name from `tabWhatsApp Message` where account = %s limit %s", (old, CHUNK), pluck=True
		)
		if not names:
			break
		frappe.db.sql(
			"""update `tabWhatsApp Message`
			set account = %(new)s, device = %(device)s,
			    dedupe_key = if(dedupe_key is null or whatsapp_message_id is null, dedupe_key,
			                    concat(%(device)s, ':', %(new)s, ':', whatsapp_message_id))
			where name in %(names)s""",
			{"new": new.name, "device": new.device, "names": tuple(names)},
		)
		_commit()

	# 3. Contacts: the freshly synced ones win; earlier ones not synced again move over.
	frappe.db.sql(
		"""delete o from `tabWhatsApp Contact` o
		inner join `tabWhatsApp Contact` n on n.account = %(new)s and n.jid = o.jid
		where o.account = %(old)s""",
		{"old": old, "new": new.name},
	)
	frappe.db.sql("update `tabWhatsApp Contact` set account = %s where account = %s", (new.name, old))
	frappe.db.delete("WhatsApp Account", {"name": old})
	_commit()


# ---------------------------------------------------------------- one record per number


def request_split(account, old_phone, cutoff):
	frappe.enqueue(
		"whatsapp_connector.accounts.split_previous_number",
		queue="long",
		timeout=3600,
		job_id=f"whatsapp_split::{account}::{old_phone}",
		deduplicate=True,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		account=account,
		old_phone=old_phone,
		cutoff=str(cutoff),
	)


def split_previous_number(account, old_phone, cutoff):
	"""A computer linked a different phone number under the same account record
	(older desktop apps re-used it after a logout on the phone). Everything stored
	before the switch belongs to the earlier number: move it to its own account
	(kept, marked as no longer linked) so each number has its own chats."""
	from frappe.utils import get_datetime

	cutoff = get_datetime(cutoff)
	acc = frappe.get_doc("WhatsApp Account", account)
	archive = frappe.db.get_value(
		"WhatsApp Account", {"user": acc.user, "phone_number": old_phone, "name": ("!=", account)}
	)
	if not archive:
		archive = (
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_id": str(uuid.uuid4()),
					"device": acc.device,
					"user": acc.user,
					"label": acc.label,
					"phone_number": old_phone,
					"status": "logged_out",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	while True:
		names = frappe.db.sql(
			"select name from `tabWhatsApp Message` where account = %s and creation < %s limit %s",
			(account, cutoff, CHUNK),
			pluck=True,
		)
		if not names:
			break
		frappe.db.sql(
			"""update `tabWhatsApp Message`
			set account = %(archive)s,
			    dedupe_key = if(dedupe_key is null or whatsapp_message_id is null, dedupe_key,
			                    concat(device, ':', %(archive)s, ':', whatsapp_message_id))
			where name in %(names)s""",
			{"archive": archive, "names": tuple(names)},
		)
		_commit()
	# Contacts synced only by the earlier number go with it.
	frappe.db.sql(
		"update `tabWhatsApp Contact` set account = %s where account = %s and modified < %s",
		(archive, account, cutoff),
	)
	_commit()
	frappe.publish_realtime("whatsapp_chat_update", {"account": account, "bulk": True}, user=acc.user)
	return archive
