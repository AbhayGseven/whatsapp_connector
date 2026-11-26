"""Scheduled maintenance (every minute, see hooks.py)."""

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from whatsapp_connector.utils import get_settings, notify_device, retry_on_conflict


@retry_on_conflict
def mark_offline_devices():
	"""Devices that stopped sending heartbeats are shown as Offline."""
	cutoff = add_to_date(now_datetime(), seconds=-get_settings().offline_after)
	stale = frappe.get_all(
		"WhatsApp Device",
		filters={"connection_status": "Online", "last_heartbeat": ("<", cutoff)},
		pluck="name",
	)
	for name in stale:
		frappe.db.set_value("WhatsApp Device", name, "connection_status", "Offline", update_modified=False)
		frappe.db.set_value(
			"WhatsApp Account", {"device": name, "status": ("!=", "logged_out")}, "status", "unknown", update_modified=False
		)  # unlinked numbers stay unlinked
		from whatsapp_connector.api.login import mark_connection_offline

		mark_connection_offline(name)


@retry_on_conflict
def release_expired_leases():
	"""Jobs claimed by a device that never reported back go back to the queue
	(or fail once the attempt limit is reached). The desktop app keeps the IDs of
	jobs it already executed, so a re-delivered job is not sent twice."""
	settings = get_settings()
	expired = frappe.get_all(
		"WhatsApp Message",
		filters={"direction": "Outgoing", "status": "Sending", "lease_expires_at": ("<", now_datetime())},
		pluck="name",
	)
	woken = set()
	for name in expired:
		doc = frappe.get_doc("WhatsApp Message", name)
		doc.lease_expires_at = None
		if (doc.attempts or 0) >= settings.max_send_attempts:
			doc.status = "Failed"
			doc.error = _("The device did not report a result before the lease expired.")
		else:
			doc.status = "Queued"
			woken.add(doc.device)
		doc.save(ignore_permissions=True)
	for device in woken - {None}:
		notify_device(device)
