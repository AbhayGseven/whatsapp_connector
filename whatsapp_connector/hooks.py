app_name = "whatsapp_connector"
app_title = "WhatsApp Connector"
app_publisher = "Gseven Computer Business"
app_description = "ERPNext side of the WhatsApp ERP Connector desktop bridge: devices, incoming messages and outgoing send jobs"
app_email = "developer@gseven.in"
app_license = "mit"

required_apps = ["frappe"]

# Installation
# ------------
before_install = "whatsapp_connector.install.before_install"
after_install = "whatsapp_connector.install.after_install"
after_migrate = "whatsapp_connector.install.after_migrate"

# Authentication
# --------------
# "Authorization: WhatsAppDevice <uuid>:<token>" for enrolled desktop connectors.
auth_hooks = ["whatsapp_connector.auth.authenticate_device"]


# Scheduled Tasks
# ---------------
scheduler_events = {
	"cron": {
		"* * * * *": [
			"whatsapp_connector.tasks.mark_offline_devices",
			"whatsapp_connector.tasks.release_expired_leases",
		],
	},
}

# Live updates for the WhatsApp Chat page
doc_events = {
	# WhatsApp sent automatically for the DocTypes in WhatsApp Connector Settings.
	"*": {
		"after_insert": "whatsapp_connector.api.document_send.on_document_event",
		"on_submit": "whatsapp_connector.api.document_send.on_document_event",
		"on_cancel": "whatsapp_connector.api.document_send.on_document_event",
	},
	# A disabled user is logged out of the desktop connector.
	"User": {"on_update": "whatsapp_connector.api.login.on_user_update"},
	"WhatsApp Message": {
		"after_insert": "whatsapp_connector.api.chat.publish_chat_update",
		"on_update": "whatsapp_connector.api.chat.publish_chat_update",
	}
}

# Business integrations
# ---------------------
# React to messages from your own app's hooks.py, for example:
#
# doc_events = {
# 	"WhatsApp Message": {
# 		"after_insert": "your_app.whatsapp.on_incoming",   # new incoming message
# 		"on_update": "your_app.whatsapp.on_status_change",   # Sent / Delivered / Read / Failed
# 	}
# }

# A WhatsApp User sees only the chats/contacts of the computers assigned to them
# (WhatsApp Device.assigned_user); managers see all. See whatsapp_connector/access.py.
permission_query_conditions = {
	"WhatsApp Connection User": "whatsapp_connector.access.connection_user_query",
	"WhatsApp Message": "whatsapp_connector.access.message_query",
	"WhatsApp Contact": "whatsapp_connector.access.contact_query",
	"WhatsApp Account": "whatsapp_connector.access.account_query",
}

has_permission = {
	"WhatsApp Connection User": "whatsapp_connector.access.has_connection_user_permission",
	"WhatsApp Message": "whatsapp_connector.access.has_account_permission",
	"WhatsApp Contact": "whatsapp_connector.access.has_account_permission",
	"WhatsApp Account": "whatsapp_connector.access.has_account_permission",
}
