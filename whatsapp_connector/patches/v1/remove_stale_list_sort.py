"""Reset saved list/report sorting that points at a field WhatsApp doctypes no longer have.

Users' saved list settings (and Property Setters) could still sort WhatsApp Message by
`received_at`, a field from an older version. Frappe v16 refuses to sort by a field that
does not exist ("You do not have permission to access field: WhatsApp Message.received_at"),
so the list would not open for those users. Their saved settings for that list are reset
to the defaults; nothing else is touched.
"""

import json
import re

import frappe
from frappe import safe_decode


def _app_doctypes():
	return frappe.get_all("DocType", filters={"module": "WhatsApp Connector"}, pluck="name")


def _sort_fields(view):
	"""Field names a saved view sorts by (List: sort_by, Report: order_by)."""
	fields = []
	if not isinstance(view, dict):
		return fields
	if view.get("sort_by"):
		fields.append(str(view["sort_by"]))
	for part in str(view.get("order_by") or "").split(","):
		match = re.search(r"`?([a-zA-Z0-9_]+)`?\s*(asc|desc)?\s*$", part.strip(), re.I)
		if match:
			fields.append(match.group(1))
	return fields


def _is_stale(data, valid):
	try:
		settings = json.loads(data or "{}")
	except Exception:
		return True
	if not isinstance(settings, dict):
		return True
	for view in settings.values():
		if any(field not in valid for field in _sort_fields(view)):
			return True
	return False


def execute():
	for doctype in _app_doctypes():
		valid = set(frappe.get_meta(doctype).get_valid_columns())

		# Default sort changed for everyone through Customize Form.
		for name, value in frappe.get_all(
			"Property Setter",
			filters={"doc_type": doctype, "property": "sort_field"},
			fields=["name", "value"],
			as_list=True,
		):
			if value not in valid:
				frappe.delete_doc("Property Setter", name, ignore_permissions=True, force=True)

		# Each user's saved list settings, in the database and in the cache
		# (the cache is written back to the database, so both must go).
		for user, data in frappe.db.sql(
			"select `user`, `data` from `__UserSettings` where `doctype`=%s", doctype
		):
			if _is_stale(data, valid):
				frappe.db.sql(
					"delete from `__UserSettings` where `doctype`=%s and `user`=%s", (doctype, user)
				)
				frappe.cache.hdel("_user_settings", f"{doctype}::{user}")
		for key, data in (frappe.cache.hgetall("_user_settings") or {}).items():
			key = safe_decode(key)
			if key.startswith(f"{doctype}::") and _is_stale(safe_decode(data), valid):
				frappe.cache.hdel("_user_settings", key)

		frappe.clear_cache(doctype=doctype)
