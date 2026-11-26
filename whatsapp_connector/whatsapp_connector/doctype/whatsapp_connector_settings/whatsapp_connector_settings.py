# Copyright (c) 2026, Gseven Computer Business and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document


class WhatsAppConnectorSettings(Document):
	def validate(self):
		self.default_country_code = re.sub(r"\D", "", self.default_country_code or "")

		minimums = {
			"heartbeat_interval": 10,
			"poll_interval": 5,
			"job_lease_seconds": 30,
			"max_send_attempts": 1,
			"max_media_size_mb": 1,
		}
		for field, minimum in minimums.items():
			if (self.get(field) or 0) < minimum:
				frappe.throw(
					_("{0} must be at least {1}.").format(_(self.meta.get_label(field)), minimum)
				)

		if (self.offline_after or 0) < 2 * self.heartbeat_interval:
			frappe.throw(_("Mark Device Offline After must be at least twice the Heartbeat Interval."))
		if self.max_media_size_mb > 100:
			frappe.throw(_("Max Media Size cannot exceed 100 MB (WhatsApp's own limit)."))

		self.realtime_url = (self.realtime_url or "").strip().rstrip("/") or None
		if self.realtime_url and not re.match(r"^(https?|wss?)://[^/\s]+$", self.realtime_url):
			frappe.throw(_("Realtime URL must look like https://host or http://host:9000 (no path)."))
