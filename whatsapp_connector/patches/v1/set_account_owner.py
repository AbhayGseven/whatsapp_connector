"""WhatsApp Account.user (owner) for accounts created before the field existed:
the user the computer is assigned to."""

import frappe


def execute():
	frappe.db.sql(
		"""update `tabWhatsApp Account` a
		inner join `tabWhatsApp Device` d on d.name = a.device
		set a.user = d.assigned_user
		where (a.user is null or a.user = '') and d.assigned_user is not null"""
	)
