import hashlib
import json
import re
from datetime import UTC, datetime

import frappe
from frappe import _
from frappe.utils import convert_utc_to_system_timezone

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

ROLE_MANAGER = "WhatsApp Manager"
ROLE_USER = "WhatsApp User"
ROLE_DEVICE = "WhatsApp Device"

# Realtime events published to the device's integration user (room "user:<user>").
EVENT_JOBS_AVAILABLE = "whatsapp_connector_jobs"
EVENT_DEVICE_STATUS = "whatsapp_connector_device_status"


# Internal user that every token-authenticated device acts as (Website User,
# WhatsApp Device role only, no password, no desk access).
SERVICE_USER = "whatsapp-connector@service.invalid"


def get_settings():
	return frappe.get_cached_doc("WhatsApp Connector Settings")


def hash_secret(value):
	"""Device tokens and enrollment codes are high-entropy random values, so a
	plain SHA-256 is sufficient; only hashes are ever stored."""
	return hashlib.sha256(value.encode()).hexdigest()


def ensure_service_user():
	if not frappe.db.exists("User", SERVICE_USER):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": SERVICE_USER,
				"first_name": "WhatsApp Connector",
				"user_type": "Website User",
				"send_welcome_email": 0,
				"enabled": 1,
				"roles": [{"role": ROLE_DEVICE}],
			}
		)
		user.flags.ignore_password_policy = True
		user.insert(ignore_permissions=True)
	if frappe.db.get_single_value("WhatsApp Connector Settings", "service_user") != SERVICE_USER:
		frappe.db.set_single_value("WhatsApp Connector Settings", "service_user", SERVICE_USER)
	return SERVICE_USER


def validate_uuid(value, label="ID"):
	value = (value or "").strip().lower() if isinstance(value, str) else ""
	if not UUID_RE.match(value):
		frappe.throw(_("Invalid {0}.").format(label), frappe.ValidationError)
	return value


def normalize_phone(raw, country_code=None):
	"""Digits-only international number, e.g. '+91 98765-43210' -> '919876543210'."""
	digits = re.sub(r"\D", "", str(raw or ""))
	if digits.startswith("00"):
		digits = digits[2:]
	if country_code is None:
		country_code = get_settings().default_country_code
	country_code = re.sub(r"\D", "", country_code or "")
	if len(digits) == 10 and country_code:
		digits = country_code + digits
	if not 7 <= len(digits) <= 15:
		frappe.throw(_("{0} is not a valid WhatsApp number.").format(frappe.bold(raw)), frappe.ValidationError)
	return digits


def parse_json_arg(value, default):
	"""Whitelisted methods receive JSON bodies already parsed, form posts as strings."""
	if value is None or value == "":
		return default
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			frappe.throw(_("Invalid JSON payload."), frappe.ValidationError)
	return value


def to_bool(value):
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def epoch_to_datetime(value):
	"""Epoch seconds or milliseconds (UTC) -> naive datetime in the system timezone."""
	if value in (None, ""):
		return None
	try:
		ts = float(value)
	except (TypeError, ValueError):
		return None
	if ts > 1e12:  # milliseconds
		ts /= 1000
	utc = datetime.fromtimestamp(ts, tz=UTC)
	return convert_utc_to_system_timezone(utc).replace(tzinfo=None)


def notify_device(device_name, event=EVENT_JOBS_AVAILABLE, payload=None):
	"""Wake the desktop connector over Frappe realtime. Delivery is best-effort;
	devices also poll, so a missed event only delays a job."""
	user = frappe.db.get_value("WhatsApp Device", device_name, "user")
	if user:
		frappe.publish_realtime(
			event,
			{"device_uuid": device_name, **(payload or {})},
			user=user,
			after_commit=True,
		)


def retry_on_conflict(fn=None, *, attempts=4):
	"""Retry an idempotent API call when MariaDB rejects it because another request
	changed the same rows at the same moment (deadlock, or error 1020 "record has
	changed" with innodb_snapshot_isolation). Frappe would otherwise answer 409
	"concurrent conflicting request". The whole transaction is rolled back and the
	call runs again, so only use it on calls that are safe to repeat."""
	import functools
	import random
	import time

	def decorate(func):
		@functools.wraps(func)
		def wrapper(*args, **kwargs):
			for attempt in range(1, attempts + 1):
				try:
					return func(*args, **kwargs)
				except frappe.QueryDeadlockError:
					if attempt == attempts:
						raise
					frappe.db.rollback()
					frappe.clear_messages()
					time.sleep(0.05 * attempt + random.random() * 0.1)

		return wrapper

	return decorate(fn) if fn else decorate
