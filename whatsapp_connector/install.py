import frappe

from whatsapp_connector.utils import ROLE_DEVICE, ROLE_MANAGER, ROLE_USER, ensure_service_user

ROLES = {
	ROLE_MANAGER: {"desk_access": 1},
	ROLE_USER: {"desk_access": 1},
	# Desktop connectors: API access only, no desk.
	ROLE_DEVICE: {"desk_access": 0},
}


def ensure_roles():
	for role_name, values in ROLES.items():
		if not frappe.db.exists("Role", role_name):
			frappe.get_doc({"doctype": "Role", "role_name": role_name, **values}).insert(ignore_permissions=True)


# Composite indexes for the chat page and sync lookups (DocType JSON only supports
# single-column indexes). Idempotent: add_index skips existing ones.
INDEXES = {
	"WhatsApp Message": [
		["account", "conversation_key", "creation"],
		["account", "chat_jid"],
		["account", "whatsapp_message_id"],
		["account", "message_type", "quoted_message_id"],
	],
	"WhatsApp Contact": [["account", "phone"], ["account", "lid"], ["account", "jid"]],
}


def ensure_indexes():
	for doctype, indexes in INDEXES.items():
		for columns in indexes:
			frappe.db.add_index(doctype, columns)


def before_install():
	ensure_roles()


def after_install():
	ensure_service_user()
	ensure_indexes()
	from whatsapp_connector.api.document_send import ensure_default_doctypes, sync_server_scripts

	ensure_default_doctypes()
	sync_server_scripts()


def after_migrate():
	ensure_roles()
	ensure_service_user()
	ensure_indexes()
	from whatsapp_connector.api.document_send import sync_server_scripts

	sync_server_scripts()
