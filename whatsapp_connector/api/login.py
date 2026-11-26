"""Desktop connector login with an ERPNext user's email and password.

1. The user enters the ERPNext URL, their email and password in the desktop app.
   ``login`` checks them exactly like the ERPNext login page (failed-attempt
   tracking, disabled users, lock-out) and requires a WhatsApp role.
2. ERPNext issues a random access token for that computer. It is stored on the
   user's **WhatsApp Connection User** row (visible) and, hashed, on the
   **WhatsApp Device** that the auth hook checks (whatsapp_connector/auth.py).
   The password itself is never stored, by ERPNext or by the app.
3. One computer per user: logging in elsewhere logs the previous computer out.
   One WhatsApp per computer: the device may hold a single WhatsApp Account.

Logging out - from the app, with "Remove Access Token", by deleting the
Connection User row or by disabling the user - clears the token, tells the
computer to unlink WhatsApp, and deletes that computer's chats and contacts
from ERPNext (background job).
"""

import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

from whatsapp_connector.api.device import ConnectorDisabled, _client_config
from whatsapp_connector.utils import (
	EVENT_DEVICE_STATUS,
	ROLE_MANAGER,
	ROLE_USER,
	ensure_service_user,
	get_settings,
	hash_secret,
	notify_device,
	validate_uuid,
)

CONNECTION_DOCTYPE = "WhatsApp Connection User"
LOGIN_ROLES = {"System Manager", ROLE_MANAGER, ROLE_USER}


class DeviceDisabled(frappe.PermissionError):
	http_status_code = 403


def _clip(value, length):
	return str(value)[:length] if value not in (None, "") else None


def _authenticate(usr, pwd):
	"""Same checks as the ERPNext login page. Returns the user name."""
	from frappe.auth import LoginManager
	from frappe.twofactor import should_run_2fa

	# The app asks for an email. Frappe's login matches the user ID (name), which
	# differs from the email for some users (e.g. "Administrator", whose email is
	# admin@example.com): accept the email address too.
	usr = (usr or "").strip()
	if usr and not frappe.db.exists("User", usr):
		usr = frappe.db.get_value("User", {"email": usr}, "name") or usr

	manager = object.__new__(LoginManager)
	manager.user = None
	manager.authenticate(usr, pwd)  # raises frappe.AuthenticationError
	user = manager.user
	if should_run_2fa(user):
		frappe.throw(
			_("Two-factor authentication is enabled for this user, which the WhatsApp ERP Connector does not support."),
			frappe.AuthenticationError,
		)
	if not LOGIN_ROLES.intersection(frappe.get_roles(user)):
		frappe.throw(
			_("User {0} needs the WhatsApp User role to use the WhatsApp ERP Connector.").format(user),
			frappe.PermissionError,
		)
	return user


def _connection(user):
	if frappe.db.exists(CONNECTION_DOCTYPE, user):
		return frappe.get_doc(CONNECTION_DOCTYPE, user)
	doc = frappe.new_doc(CONNECTION_DOCTYPE)
	doc.user = user
	return doc


def _whatsapp_of(device_name):
	row = frappe.db.get_value(
		"WhatsApp Account",
		{"device": device_name},
		["phone_number", "status"],
		as_dict=True,
		order_by="modified desc",
	)
	return row or frappe._dict()


def revoke_device(device_name, reason="logout", notify=True, update_connection=True):
	"""Log a computer out: clear its token and (optionally) tell the app to unlink
	WhatsApp. Chats stay in ERPNext with their owner. Safe to call repeatedly."""
	device = frappe.db.get_value("WhatsApp Device", device_name, ["name", "assigned_user", "token_hash"], as_dict=True)
	if not device:
		return False
	frappe.db.set_value("WhatsApp Device", device.name, {"token_hash": None, "token_issued_on": None})
	if update_connection and device.assigned_user and frappe.db.exists(CONNECTION_DOCTYPE, device.assigned_user):
		conn = frappe.get_doc(CONNECTION_DOCTYPE, device.assigned_user)
		if conn.device == device.name:
			conn.update(
				{
					"access_token": None,
					"token_issued_on": None,
					"status": "Disconnected",
					"last_logout": now_datetime(),
					"whatsapp_status": "Disconnected",
				}
			)
			conn.save(ignore_permissions=True)
	# Chats are kept; the computer unlinks WhatsApp, so the accounts are no longer linked.
	frappe.db.set_value("WhatsApp Account", {"device": device.name}, {"status": "logged_out", "is_default": 0})
	if notify:
		notify_device(device.name, EVENT_DEVICE_STATUS, {"status": "token_reset", "reason": reason})
	return True


# ---------------------------------------------------------------- desktop app


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=10 * 60)
def login(usr, pwd, device_uuid, device_name=None, hostname=None, platform=None, app_version=None):
	"""Log a computer in with an ERPNext user's email (or username) and password.
	Returns the computer's access token once."""
	return _login(usr, pwd, device_uuid, device_name, hostname, platform, app_version)


def _login(usr, pwd, device_uuid, device_name=None, hostname=None, platform=None, app_version=None):
	if not get_settings().enabled:
		frappe.throw(_("WhatsApp Connector is disabled on this ERPNext site."), ConnectorDisabled)
	uuid = validate_uuid(device_uuid, "device UUID")
	user = _authenticate(usr, pwd)

	existing = frappe.db.get_value("WhatsApp Device", uuid, ["name", "status", "assigned_user"], as_dict=True)
	if existing and existing.status == "Disabled":
		frappe.throw(_("This computer has been disabled in ERPNext. Ask a WhatsApp Manager."), DeviceDisabled)

	# The previous user is logged out here; their chats stay theirs (WhatsApp Account.user).
	if existing and existing.assigned_user and existing.assigned_user != user:
		revoke_device(existing.name, reason="other_user_login", notify=False)

	# One computer per user: log the previous computer out.
	conn = _connection(user)
	if conn.device and conn.device != uuid and frappe.db.exists("WhatsApp Device", conn.device):
		revoke_device(conn.device, reason="logged_in_elsewhere", update_connection=False)

	service_user = ensure_service_user()
	token = secrets.token_urlsafe(32)
	now = now_datetime()

	if existing:
		device = frappe.get_doc("WhatsApp Device", uuid)
	else:
		device = frappe.new_doc("WhatsApp Device")
		device.device_uuid = uuid
		device.registered_on = now
	device.update(
		{
			"user": service_user,
			"assigned_user": user,
			"status": "Approved",
			"auth_method": "User Login",
			"token_hash": hash_secret(token),
			"token_issued_on": now,
			"device_name": _clip(device_name, 140) or device.device_name or _clip(hostname, 140) or uuid[:8],
			"hostname": _clip(hostname, 140),
			"platform": _clip(platform, 140),
			"app_version": _clip(app_version, 140),
			"last_ip": frappe.local.request_ip,
		}
	)
	device.save(ignore_permissions=True) if existing else device.insert(ignore_permissions=True)

	whatsapp = _whatsapp_of(device.name)
	conn.update(
		{
			"device": device.name,
			"access_token": token,
			"token_issued_on": now,
			"last_login": now,
			"status": "Connected",
			"whatsapp_number": whatsapp.phone_number,
			"whatsapp_status": whatsapp_label(whatsapp.status),
		}
	)
	conn.save(ignore_permissions=True) if not conn.is_new() else conn.insert(ignore_permissions=True)

	return {
		"device_uuid": device.name,
		"device_token": token,
		"device_name": device.device_name,
		"status": device.status,
		"user": user,
		"full_name": frappe.db.get_value("User", user, "full_name") or user,
		"config": _client_config(),
		"server_time": now,
	}


@frappe.whitelist(methods=["POST"])
def logout(device_uuid):
	"""The user logged out in the desktop app (device token required)."""
	from whatsapp_connector.api.device import _get_device

	device = _get_device(device_uuid, require_approved=False)
	revoke_device(device.name, reason="logout", notify=False)
	return {"logged_out": True}


# ---------------------------------------------------------------- managers


def _require_manager():
	if not {"System Manager", ROLE_MANAGER}.intersection(frappe.get_roles()):
		frappe.throw(_("Only a WhatsApp Manager can log users out."), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def remove_access_token(user):
	"""Remove a user's access token: their computer is logged out of ERPNext and
	WhatsApp. They can log in again with their email and password."""
	_require_manager()
	conn = frappe.get_doc(CONNECTION_DOCTYPE, user)
	if conn.device:
		revoke_device(conn.device, reason="token_removed")
	else:
		conn.update({"access_token": None, "token_issued_on": None, "status": "Disconnected", "whatsapp_status": "Disconnected"})
		conn.save(ignore_permissions=True)
	return {"user": user, "logged_out": True}


@frappe.whitelist(methods=["POST"])
def reset_device_token(device):
	"""Log a computer out from its WhatsApp Device record."""
	_require_manager()
	revoke_device(device, reason="token_reset")
	return {"device": device, "token_reset": True}


# ---------------------------------------------------------------- hooks


def on_user_update(doc, method=None):
	"""doc_events: a disabled user is logged out of the connector immediately."""
	if doc.enabled or not frappe.db.exists(CONNECTION_DOCTYPE, doc.name):
		return
	device = frappe.db.get_value(CONNECTION_DOCTYPE, doc.name, "device")
	if device:
		revoke_device(device, reason="user_disabled")


def whatsapp_label(account_status):
	"""WhatsApp Account status (from the app) -> what the Connection User row shows."""
	if account_status == "connected":
		return "Connected"
	if account_status in ("connecting", "qr", "reconnecting"):
		return "Connecting"
	return "Disconnected"


def _set_connection(user, values):
	"""Save only real changes (rows are versioned and the list refreshes on save)."""
	doc = frappe.get_doc(CONNECTION_DOCTYPE, user)
	if all(doc.get(k) == v for k, v in values.items()):
		return
	doc.update(values)
	doc.save(ignore_permissions=True)


def update_connection_whatsapp(device_name, phone_number, account_status):
	"""Device heartbeat: the computer is connected to ERPNext; show its WhatsApp
	number and whether WhatsApp is connected (account_status None = no WhatsApp linked)."""
	user = frappe.db.get_value("WhatsApp Device", device_name, "assigned_user")
	if not user or not frappe.db.exists(CONNECTION_DOCTYPE, user):
		return
	current = frappe.db.get_value(CONNECTION_DOCTYPE, user, ["device", "access_token", "whatsapp_number"], as_dict=True)
	if current.device != device_name or not current.access_token:
		return
	_set_connection(
		user,
		{
			"status": "Connected",
			"whatsapp_status": whatsapp_label(account_status),
			# Keep the last known number while disconnected; clear it once WhatsApp is unlinked.
			"whatsapp_number": phone_number or (current.whatsapp_number if account_status else None),
		},
	)


def mark_connection_offline(device_name):
	"""The computer stopped sending heartbeats (closed, shut down, no network)."""
	user = frappe.db.get_value("WhatsApp Device", device_name, "assigned_user")
	if not user or not frappe.db.exists(CONNECTION_DOCTYPE, user):
		return
	if frappe.db.get_value(CONNECTION_DOCTYPE, user, "device") != device_name:
		return
	_set_connection(user, {"status": "Disconnected", "whatsapp_status": "Disconnected"})
