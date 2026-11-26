"""Send a WhatsApp message automatically when a business document is created,
submitted or cancelled.

WhatsApp Connector Settings > DocTypes lists the DocTypes and, per DocType, when
to send (Send When), the mobile field, the message template and the PDF settings.
The message goes out from the WhatsApp number of the user who saved / submitted
the document (else the default sending account), is linked to the document, and a
comment on the document records the result. The work runs in the background after
the save is committed, so saving never waits for the PDF or the message.
"""

import frappe
from frappe import _
from frappe.utils import cint

from whatsapp_connector.api.messaging import queue_message

# Where a document usually keeps a mobile number, and party links to follow.
MOBILE_FIELDS = ("contact_mobile", "mobile_no", "mobile", "whatsapp_no", "contact_phone", "phone", "phone_no")
PARTY_LINKS = (("customer", "Customer"), ("supplier", "Supplier"), ("party_name", None), ("party", None))

# (DocType, send when, attach PDF, message template)
DEFAULT_ROWS = [
	("Sales Invoice", "Submit", 1, "Dear {{ doc.customer_name }}, please find invoice {{ doc.name }} of {{ doc.get_formatted('grand_total') }} attached."),
	("Sales Order", "Submit", 1, "Dear {{ doc.customer_name }}, thank you for your order {{ doc.name }}. Please find the details attached."),
	("Quotation", "Submit", 1, "Dear {{ doc.customer_name or doc.party_name }}, please find our quotation {{ doc.name }} attached."),
	("Delivery Note", "Submit", 1, "Dear {{ doc.customer_name }}, your goods have been dispatched with delivery note {{ doc.name }}."),
	("Purchase Order", "Submit", 1, "Dear {{ doc.supplier_name }}, please find purchase order {{ doc.name }} attached."),
	("Customer", "New Document", 0, "Dear {{ doc.customer_name }}, "),
	("Supplier", "New Document", 0, "Dear {{ doc.supplier_name }}, "),
]


def ensure_default_doctypes():
	"""Fill the DocTypes table once (if empty); give existing rows a Send When."""
	settings = frappe.get_single("WhatsApp Connector Settings")
	defaults = {row[0]: row for row in DEFAULT_ROWS}
	if settings.get("send_doctypes"):
		changed = False
		for row in settings.send_doctypes:
			if not row.get("send_on") and row.document_type in defaults:
				row.send_on = defaults[row.document_type][1]
				changed = True
		if not changed:
			return
	else:
		for doctype, send_on, attach_pdf, template in DEFAULT_ROWS:
			if frappe.db.exists("DocType", doctype):
				settings.append(
					"send_doctypes",
					{"document_type": doctype, "send_on": send_on, "attach_pdf": attach_pdf, "message_template": template},
				)
	settings.flags.ignore_permissions = True
	settings.save()


EVENTS = {"after_insert": "New Document", "on_submit": "Submit", "on_cancel": "Cancel"}


def _rule(doctype, send_on):
	if doctype.startswith("WhatsApp "):
		return None
	for row in frappe.get_cached_doc("WhatsApp Connector Settings").get("send_doctypes") or []:
		if row.document_type == doctype and row.send_on == send_on:
			return row
	return None


def on_document_event(doc, method=None):
	"""doc_events "*": queue the WhatsApp in the background after the save commits."""
	if frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	send_on = EVENTS.get(method)
	if not send_on or not _rule(doc.doctype, send_on):
		return
	frappe.enqueue(
		"whatsapp_connector.api.document_send.send_for_document",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		doctype=doc.doctype,
		name=doc.name,
		send_on=send_on,
		user=frappe.session.user,
	)


def _mobile_of(doc, field=None):
	candidates = [field] if field else []
	candidates += [f for f in MOBILE_FIELDS if f != field]
	for fieldname in candidates:
		if fieldname and doc.meta.has_field(fieldname) and doc.get(fieldname):
			return doc.get(fieldname)
	# Follow the contact person or the party (customer / supplier / party).
	if doc.meta.has_field("contact_person") and doc.get("contact_person"):
		mobile = frappe.db.get_value("Contact", doc.contact_person, ["mobile_no", "phone"])
		if mobile and (mobile[0] or mobile[1]):
			return mobile[0] or mobile[1]
	for fieldname, target in PARTY_LINKS:
		if not (doc.meta.has_field(fieldname) and doc.get(fieldname)):
			continue
		target = target or doc.get("party_type") or doc.get("quotation_to")
		if target in ("Customer", "Supplier") and frappe.db.exists(target, doc.get(fieldname)):
			mobile = frappe.db.get_value(target, doc.get(fieldname), "mobile_no")
			if mobile:
				return mobile
	return None


def _sending_account(user):
	"""The user's own linked WhatsApp number, else the default sending account."""
	linked = {"status": ("!=", "logged_out"), "phone_number": ("is", "set")}
	return frappe.db.get_value("WhatsApp Account", {**linked, "user": user}, "name", order_by="is_default desc") or frappe.db.get_value(
		"WhatsApp Account", {**linked, "is_default": 1}, "name"
	)


def _pdf_file(doc, print_format=None):
	pdf = frappe.get_print(doc.doctype, doc.name, print_format=print_format or None, as_pdf=True)
	safe_name = "".join(c if c.isalnum() or c in "-_." else "-" for c in doc.name)
	return (
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{safe_name}.pdf",
				"is_private": 1,
				"content": pdf,
				# Kept with the document (not deleted with the chat).
				"attached_to_doctype": doc.doctype,
				"attached_to_name": doc.name,
			}
		)
		.insert(ignore_permissions=True)
		.name  # the File record itself (several records may share one URL)
	)


def send_for_document(doctype, name, send_on, user=None):
	"""Background job: build and queue the WhatsApp for one document event."""
	rule = _rule(doctype, send_on)
	if not rule or not frappe.db.exists(doctype, name):
		return None
	doc = frappe.get_doc(doctype, name)

	def note(text):
		doc.add_comment("Comment", text=f"WhatsApp: {text}")

	mobile = _mobile_of(doc, rule.mobile_field)
	if not mobile:
		note(_("not sent - no mobile number on this {0}.").format(_(doctype)))
		return None
	account = _sending_account(user)
	if not account:
		note(_("not sent - no WhatsApp number is linked for {0} and no default sending account is set.").format(user))
		return None
	try:
		message = frappe.render_template(rule.message_template or "{{ doc.doctype }} {{ doc.name }}", {"doc": doc}).strip()
	except Exception:
		frappe.clear_messages()
		message = f"{_(doctype)} {name}"
	try:
		attachment = _pdf_file(doc, rule.print_format) if cint(rule.attach_pdf) and send_on != "Cancel" else None
		msg = queue_message(
			to=mobile,
			message=message or None,
			attachment=attachment,
			account=account,
			reference_doctype=doctype,
			reference_name=name,
			# Saving / submitting again never sends the same notice twice.
			idempotency_key=f"auto:{doctype}:{name}:{send_on}",
		)
	except Exception as e:
		frappe.clear_messages()
		note(_("not sent - {0}").format(str(e)[:200]))
		return None
	note(_("queued to +{0} ({1}).").format(msg.phone, msg.name))
	return msg.name
