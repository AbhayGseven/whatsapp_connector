"""Remove everything ERPNext holds for a WhatsApp account when it is logged out.

Requested by the desktop connector (device API `purge_account`) when the phone
unlinks the device or the account is removed in the connector. Runs as a
background job in small chunks with a commit after each, so large accounts never
lock tables or block the site. Only rows that existed when logout happened are
deleted, so a quick re-link of the same account keeps its new chats.
"""

import frappe
from frappe.utils import get_datetime

CHUNK = 1000
FILE_CHUNK = 200


def _commit():
	# Commit per chunk to keep transactions short (not inside tests, which roll back).
	if not frappe.flags.in_test:
		frappe.db.commit()


def request_purge(account, remove_account=False):
	"""Queue the purge (deduplicated per account). Returns immediately."""
	cutoff = frappe.utils.now_datetime()
	frappe.enqueue(
		"whatsapp_connector.purge.purge_account_data",
		queue="long",
		timeout=3600,
		job_id=f"whatsapp_purge::{account}",
		deduplicate=True,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		account=account,
		cutoff=str(cutoff),
		remove_account=bool(remove_account),
	)


def purge_account_data(account, cutoff, remove_account=False):
	cutoff = get_datetime(cutoff)

	# 1. Attachment files (private files on disk) of the account's messages.
	while True:
		files = frappe.db.sql(
			"""select f.name from `tabFile` f
			inner join `tabWhatsApp Message` m
			  on f.attached_to_doctype = 'WhatsApp Message' and f.attached_to_name = m.name
			where m.account = %s and m.creation <= %s
			limit %s""",
			(account, cutoff, FILE_CHUNK),
			pluck=True,
		)
		if not files:
			break
		for name in files:
			frappe.delete_doc("File", name, force=True, ignore_permissions=True, delete_permanently=True)
		_commit()

	# 2. Messages (incoming, outgoing, queued jobs, reactions) and 3. contacts.
	for doctype, date_field in (("WhatsApp Message", "creation"), ("WhatsApp Contact", "modified")):
		while True:
			names = frappe.db.sql(
				f"""select name from `tab{doctype}`
				where account = %s and `{date_field}` <= %s limit %s""",
				(account, cutoff, CHUNK),
				pluck=True,
			)
			if not names:
				break
			frappe.db.delete(doctype, {"name": ("in", names)})
			_commit()

	from whatsapp_connector.access import users_for_account

	# Collected before the account (and its device link) may be deleted below.
	recipients = users_for_account(account)
	if remove_account and frappe.db.exists("WhatsApp Account", account):
		frappe.db.delete("WhatsApp Account", {"name": account})
		_commit()

	for user in recipients:
		frappe.publish_realtime("whatsapp_chat_update", {"account": account, "purged": True}, user=user)
