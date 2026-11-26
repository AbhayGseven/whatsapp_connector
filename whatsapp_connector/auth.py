"""Per-device token authentication (registered in hooks.py as an auth hook).

Enrolled desktop connectors send

    Authorization: WhatsAppDevice <device_uuid>:<device_token>

on every REST call and on the Frappe Socket.IO connection (Frappe's realtime
server forwards the header when it verifies the socket, so the same hook
authenticates it). A valid token logs the request in as the connector service
user and records which device it is in ``frappe.local.whatsapp_device``; the
device API then only lets that device act as itself.

If the header uses this scheme but is invalid, the hook leaves the session as
Guest and Frappe answers 401.
"""

import hmac

import frappe

from whatsapp_connector.utils import UUID_RE, hash_secret

SCHEME = "whatsappdevice"
MAX_TOKEN_LENGTH = 200


def verify_device_token(device_uuid, token):
	"""Return the device row for a valid, non-disabled device token, else None."""
	device_uuid = (device_uuid or "").strip().lower()
	if not UUID_RE.match(device_uuid) or not token or len(token) > MAX_TOKEN_LENGTH:
		return None
	device = frappe.db.get_value(
		"WhatsApp Device", device_uuid, ["name", "user", "status", "token_hash", "assigned_user"], as_dict=True
	)
	if not device or not device.token_hash or device.status == "Disabled":
		return None
	if device.assigned_user and not frappe.db.get_value("User", device.assigned_user, "enabled"):
		return None
	if not hmac.compare_digest(device.token_hash, hash_secret(token)):
		return None
	return device


def authenticate_device():
	header = frappe.get_request_header("Authorization", "") or ""
	scheme, _, credentials = header.partition(" ")
	if scheme.lower() != SCHEME:
		return

	device_uuid, _, token = credentials.strip().partition(":")
	device = verify_device_token(device_uuid, token)
	if not device:
		return

	if frappe.local.login_manager.user in ("", "Guest"):
		form_dict = frappe.local.form_dict
		frappe.set_user(device.user)
		frappe.local.form_dict = form_dict
	frappe.local.whatsapp_device = device.name
