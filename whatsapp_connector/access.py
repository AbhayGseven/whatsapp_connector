"""Who may see which WhatsApp chats and contacts.

Every WhatsApp number (WhatsApp Account) belongs to the ERPNext user who linked it
(WhatsApp Account.user) - also after logout or unlinking, when its chats are kept.
A WhatsApp User sees only their own accounts: chats, messages, attachments and
contacts. WhatsApp Managers and System Managers see everything.

Enforced in three places:
- permission hooks (list views, reports, REST /api/resource, attachments),
- the chat page / messaging APIs (raw SQL, so they check explicitly),
- realtime chat events, which are sent only to the users allowed to see them.
"""

import frappe
from frappe import _

from whatsapp_connector.utils import ROLE_MANAGER

FULL_ACCESS_ROLES = {"System Manager", ROLE_MANAGER}


def sees_all(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or bool(FULL_ACCESS_ROLES.intersection(frappe.get_roles(user)))


def allowed_accounts(user=None):
	"""None = every account; otherwise the list of WhatsApp Account names."""
	user = user or frappe.session.user
	if sees_all(user):
		return None
	return frappe.get_all("WhatsApp Account", filters={"user": user}, pluck="name")


def can_access_account(account, user=None):
	allowed = allowed_accounts(user)
	return allowed is None or account in allowed


def check_account(account, user=None):
	if not can_access_account(account, user):
		frappe.throw(_("You do not have access to this WhatsApp account."), frappe.PermissionError)


def users_for_account(account):
	"""Users who should receive realtime chat events for this account."""
	users = set(
		frappe.db.sql(
			"""select distinct hr.parent from `tabHas Role` hr
			inner join `tabUser` u on u.name = hr.parent
			where hr.parenttype = 'User' and hr.role in %s and u.enabled = 1 and u.user_type = 'System User'""",
			(tuple(FULL_ACCESS_ROLES),),
			pluck=True,
		)
	)
	users.add("Administrator")
	owner = frappe.db.get_value("WhatsApp Account", account, "user")
	if owner:
		users.add(owner)
	return users


def publish_chat_event(account, payload, after_commit=False):
	for user in users_for_account(account):
		frappe.publish_realtime("whatsapp_chat_update", payload, user=user, after_commit=after_commit)


# ---------------------------------------------------------------- permission hooks


def _account_condition(user, column):
	if sees_all(user):
		return ""
	user = frappe.db.escape(user)
	return f"""{column} in (select a.name from `tabWhatsApp Account` a where a.user = {user})"""


def message_query(user=None, doctype=None):
	return _account_condition(user or frappe.session.user, "`tabWhatsApp Message`.`account`")


def contact_query(user=None, doctype=None):
	return _account_condition(user or frappe.session.user, "`tabWhatsApp Contact`.`account`")


def account_query(user=None, doctype=None):
	return _account_condition(user or frappe.session.user, "`tabWhatsApp Account`.`name`")


def has_account_permission(doc, ptype=None, user=None, debug=False):
	"""WhatsApp Message / WhatsApp Contact (by account) and WhatsApp Account itself.
	Returning True defers to the role permissions; False denies."""
	user = user or frappe.session.user
	if sees_all(user):
		return True
	account = doc.name if doc.doctype == "WhatsApp Account" else doc.get("account")
	return bool(account) and can_access_account(account, user)


# ---------------------------------------------------------------- WhatsApp Connection User
# Holds the access token in plain text: a WhatsApp User sees only their own row.


def connection_user_query(user=None, doctype=None):
	user = user or frappe.session.user
	if sees_all(user):
		return ""
	return f"`tabWhatsApp Connection User`.`user` = {frappe.db.escape(user)}"


def has_connection_user_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	return sees_all(user) or doc.user == user
