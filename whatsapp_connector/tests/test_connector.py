# Integration tests for the device protocol and messaging API.
# Run: bench --site <site> run-tests --app whatsapp_connector

import base64
import io
import uuid

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from whatsapp_connector import tasks
from whatsapp_connector.api import device as api
from whatsapp_connector.api import login as login_api
from whatsapp_connector.api import messaging
from whatsapp_connector.auth import verify_device_token
from whatsapp_connector.utils import SERVICE_USER, normalize_phone

DEVICE_USER = "wa-device-test@example.com"
OTHER_DEVICE_USER = "wa-device-other@example.com"
NO_ROLE_USER = "wa-norole-test@example.com"


def make_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "WA Test", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if not user.enabled:  # an interrupted earlier run may have left it disabled
		frappe.db.set_value("User", email, "enabled", 1)
		user = frappe.get_doc("User", email)
	if roles:
		user.add_roles(*roles)


def new_id():
	return str(uuid.uuid4())


def tiny_image(fmt):
	"""Real image bytes: Frappe parses images on upload to strip EXIF data."""
	from PIL import Image

	buffer = io.BytesIO()
	Image.new("RGB", (4, 4), (18, 140, 94)).save(buffer, format=fmt)
	return buffer.getvalue()


class TestWhatsAppConnector(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(DEVICE_USER, ["WhatsApp Device"])
		make_user(OTHER_DEVICE_USER, ["WhatsApp Device"])
		make_user(NO_ROLE_USER, [])
		settings = frappe.get_single("WhatsApp Connector Settings")
		settings.update(
			{"enabled": 1, "auto_approve_devices": 0, "default_country_code": "91", "max_send_attempts": 5}
		)
		settings.save()

	def tearDown(self):
		frappe.local.whatsapp_device = None
		frappe.set_user("Administrator")

	# ------------------------------------------------------------ helpers

	def approved_device(self, phone="+91 98765 43210"):
		device_id, account_id = new_id(), new_id()
		frappe.set_user(DEVICE_USER)
		api.register_device(device_id, device_name="PC-A", hostname="pc-a", platform="win32", app_version="0.1.0")
		frappe.set_user("Administrator")
		frappe.db.set_value("WhatsApp Device", device_id, "status", "Approved")
		frappe.set_user(DEVICE_USER)
		api.heartbeat(
			device_id,
			whatsapp_status="connected",
			accounts=[{"account_id": account_id, "label": "Sales", "phone_number": phone, "status": "connected"}],
		)
		frappe.set_user("Administrator")
		return device_id, account_id

	def incoming(self, account_id, message_id, **extra):
		return {
			"account_id": account_id,
			"message_id": message_id,
			"chat_jid": "919811122233@s.whatsapp.net",
			"sender_jid": "919811122233@s.whatsapp.net",
			"sender_phone": "919811122233",
			"sender_name": "Customer",
			"message_type": "text",
			"content": "Hello from WhatsApp",
			"timestamp": 1763700000,
			**extra,
		}

	# ------------------------------------------------------------ tests

	def test_normalize_phone(self):
		self.assertEqual(normalize_phone("+91 98765-43210"), "919876543210")
		self.assertEqual(normalize_phone("9876543210"), "919876543210")
		self.assertEqual(normalize_phone("0044 20 7946 0958"), "442079460958")
		with self.assertRaises(frappe.ValidationError):
			normalize_phone("12345")

	def test_register_requires_device_role(self):
		frappe.set_user(NO_ROLE_USER)
		with self.assertRaises(frappe.PermissionError):
			api.register_device(new_id())

	def test_new_device_waits_for_approval(self):
		device_id = new_id()
		frappe.set_user(DEVICE_USER)
		result = api.register_device(device_id, device_name="PC-B")
		self.assertEqual(result["status"], "Pending Approval")
		self.assertEqual(result["config"]["heartbeat_interval"], 30)

		beat = api.heartbeat(device_id, accounts=[{"account_id": new_id(), "status": "connected"}])
		self.assertEqual(beat["status"], "Pending Approval")
		self.assertFalse(frappe.db.exists("WhatsApp Account", {"device": device_id}))

		with self.assertRaises(api.DeviceNotApproved):
			api.claim_jobs(device_id)
		with self.assertRaises(api.DeviceNotApproved):
			api.push_incoming(device_id, [])

	def test_device_is_bound_to_its_user(self):
		device_id, _ = self.approved_device()
		frappe.set_user(OTHER_DEVICE_USER)
		with self.assertRaises(frappe.PermissionError):
			api.heartbeat(device_id)
		with self.assertRaises(frappe.PermissionError):
			api.register_device(device_id)

	def test_unknown_device_and_bad_uuid(self):
		frappe.set_user(DEVICE_USER)
		with self.assertRaises(api.DeviceNotRegistered):
			api.heartbeat(new_id())
		with self.assertRaises(frappe.ValidationError):
			api.heartbeat("../../etc/passwd")

	def test_heartbeat_syncs_accounts(self):
		device_id, account_id = self.approved_device()
		account = frappe.get_doc("WhatsApp Account", account_id)
		self.assertEqual(account.device, device_id)
		self.assertEqual(account.phone_number, "919876543210")
		self.assertEqual(account.status, "connected")
		self.assertEqual(frappe.db.get_value("WhatsApp Device", device_id, "connection_status"), "Online")

		# An account ID can never be taken over by another computer.
		other_device, _ = self.approved_device()
		frappe.set_user(DEVICE_USER)
		api.heartbeat(other_device, accounts=[{"account_id": account_id, "status": "error"}])
		self.assertEqual(frappe.db.get_value("WhatsApp Account", account_id, "device"), device_id)

	def test_incoming_messages_are_deduplicated(self):
		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		batch = [
			self.incoming(account_id, "MSG-1"),
			self.incoming(account_id, "MSG-LID", sender_phone=None, chat_jid="123456789012345678@lid"),
			self.incoming(new_id(), "MSG-X"),
		]
		first = api.push_incoming(device_id, batch)
		self.assertEqual([a["message_id"] for a in first["accepted"]], ["MSG-1", "MSG-LID"])
		self.assertEqual([r["message_id"] for r in first["rejected"]], ["MSG-X"])

		second = api.push_incoming(device_id, [self.incoming(account_id, "MSG-1")])
		self.assertEqual(second["accepted"], [])
		self.assertEqual(second["duplicates"][0]["name"], first["accepted"][0]["name"])

		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.count("WhatsApp Message", {"account": account_id, "whatsapp_message_id": "MSG-1"}), 1
		)
		msg = frappe.get_doc("WhatsApp Message", first["accepted"][0]["name"])
		self.assertEqual((msg.direction, msg.status, msg.phone), ("Incoming", "Received", "919811122233"))
		lid = frappe.get_doc("WhatsApp Message", first["accepted"][1]["name"])
		self.assertIsNone(lid.phone)

	def test_message_sent_from_phone_is_recorded_as_outgoing(self):
		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		result = api.push_incoming(device_id, [self.incoming(account_id, "MSG-ME", from_me=True)])
		frappe.set_user("Administrator")
		doc = frappe.get_doc("WhatsApp Message", result["accepted"][0]["name"])
		self.assertEqual((doc.direction, doc.status), ("Outgoing", "Sent"))

	def test_upload_incoming_media(self):
		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		api.push_incoming(
			device_id,
			[self.incoming(account_id, "MSG-IMG", message_type="image", media={"mimetype": "image/jpeg", "filename": "photo.jpg"})],
		)
		content = base64.b64encode(tiny_image("JPEG")).decode()
		first = api.upload_incoming_media(device_id, account_id, "MSG-IMG", "photo.jpg", content, "image/jpeg")
		again = api.upload_incoming_media(device_id, account_id, "MSG-IMG", "photo.jpg", content, "image/jpeg")
		self.assertTrue(first["file_url"].startswith("/private/files/"))
		self.assertTrue(again["duplicate"])

	def test_outgoing_text_lifecycle(self):
		device_id, account_id = self.approved_device()

		doc = messaging.queue_message("98765 01234", "Invoice ready", account=account_id, idempotency_key="k-" + new_id())
		self.assertEqual((doc.status, doc.phone, doc.command), ("Queued", "919876501234", "SEND_TEXT"))
		same = messaging.queue_message("98765 01234", "Invoice ready", account=account_id, idempotency_key=doc.idempotency_key)
		self.assertEqual(same.name, doc.name)

		frappe.set_user(DEVICE_USER)
		self.assertEqual(api.heartbeat(device_id)["pending_jobs"], 1)
		jobs = api.claim_jobs(device_id)["jobs"]
		self.assertEqual(len(jobs), 1)
		job = jobs[0]
		self.assertEqual((job["job_id"], job["to"], job["account_id"], job["attempt"]), (doc.name, "919876501234", account_id, 1))
		self.assertEqual(api.claim_jobs(device_id)["jobs"], [])  # leased, not handed out twice

		result = api.report_job_result(device_id, doc.name, success=True, whatsapp_message_id="WA-OUT-1", sent_at=1763700100)
		self.assertEqual(result["status"], "Sent")
		self.assertTrue(api.report_job_result(device_id, doc.name, success=True, whatsapp_message_id="WA-OUT-1")["duplicate"])

		api.report_receipts(device_id, [{"account_id": account_id, "message_id": "WA-OUT-1", "status": "read", "timestamp": 1763700200}])
		api.report_receipts(device_id, [{"account_id": account_id, "message_id": "WA-OUT-1", "status": "delivered"}])
		frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.status, "Read")  # never moves backwards
		self.assertIsNotNone(doc.delivered_at)
		self.assertIsNotNone(doc.read_at)

	def test_retryable_failure_then_give_up(self):
		frappe.db.set_single_value("WhatsApp Connector Settings", "max_send_attempts", 2)
		frappe.clear_document_cache("WhatsApp Connector Settings", "WhatsApp Connector Settings")
		device_id, account_id = self.approved_device()
		doc = messaging.queue_message("9876501234", "retry me", account=account_id)

		frappe.set_user(DEVICE_USER)
		api.claim_jobs(device_id)
		self.assertEqual(api.report_job_result(device_id, doc.name, success=False, error="Not connected", retryable=True)["status"], "Queued")
		api.claim_jobs(device_id)
		self.assertEqual(api.report_job_result(device_id, doc.name, success=False, error="Not connected", retryable=True)["status"], "Failed")

		frappe.set_user("Administrator")
		self.assertEqual(messaging.retry(doc.name)["status"], "Queued")

	def test_permanent_failure(self):
		device_id, account_id = self.approved_device()
		doc = messaging.queue_message("9876501234", "bad number", account=account_id)
		frappe.set_user(DEVICE_USER)
		api.claim_jobs(device_id)
		result = api.report_job_result(device_id, doc.name, success=False, error="Number is not on WhatsApp", retryable=False)
		self.assertEqual(result["status"], "Failed")

	def test_expired_lease_returns_job_to_queue(self):
		device_id, account_id = self.approved_device()
		doc = messaging.queue_message("9876501234", "lease", account=account_id)
		frappe.set_user(DEVICE_USER)
		api.claim_jobs(device_id)
		frappe.set_user("Administrator")
		frappe.db.set_value("WhatsApp Message", doc.name, "lease_expires_at", add_to_date(now_datetime(), minutes=-5))
		tasks.release_expired_leases()
		self.assertEqual(frappe.db.get_value("WhatsApp Message", doc.name, "status"), "Queued")

	def test_jobs_only_go_to_the_owning_device(self):
		device_a, account_a = self.approved_device()
		device_b, _ = self.approved_device()
		doc = messaging.queue_message("9876501234", "for A only", account=account_a)
		frappe.set_user(DEVICE_USER)
		self.assertNotIn(doc.name, [j["job_id"] for j in api.claim_jobs(device_b)["jobs"]])
		with self.assertRaises(frappe.PermissionError):
			api.report_job_result(device_b, doc.name, success=True)
		self.assertIn(doc.name, [j["job_id"] for j in api.claim_jobs(device_a)["jobs"]])

	def test_pdf_job_and_media_download(self):
		device_id, account_id = self.approved_device()
		pdf = tiny_image("PDF")  # a real one-page PDF; Frappe validates PDFs on upload
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": f"invoice-{new_id()[:8]}.pdf", "is_private": 1, "content": pdf}
		).insert(ignore_permissions=True)

		doc = messaging.queue_message("9876501234", "Your invoice", attachment=file_doc.file_url, account=account_id)
		self.assertEqual((doc.command, doc.media_mimetype), ("SEND_PDF", "application/pdf"))

		frappe.set_user(DEVICE_USER)
		job = api.claim_jobs(device_id)["jobs"][0]
		self.assertEqual(job["media"]["filename"], file_doc.file_name)
		self.assertIn("download_job_media", job["media"]["url"])

		api.download_job_media(device_id, doc.name)
		self.assertEqual(frappe.local.response.filecontent, pdf)
		self.assertEqual(frappe.local.response.type, "download")

	def test_image_attachment_command(self):
		device_id, account_id = self.approved_device()
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": f"photo-{new_id()[:8]}.png", "is_private": 1, "content": tiny_image("PNG")}
		).insert(ignore_permissions=True)
		doc = messaging.queue_message("9876501234", None, attachment=file_doc.file_url, account=account_id)
		self.assertEqual(doc.command, "SEND_IMAGE")

		# Regression: binary media must be streamed byte-for-byte (not decoded as text).
		with open(frappe.get_doc("File", file_doc.name).get_full_path(), "rb") as fh:
			on_disk = fh.read()
		frappe.set_user(DEVICE_USER)
		api.claim_jobs(device_id)
		api.download_job_media(device_id, doc.name)
		self.assertIsInstance(frappe.local.response.filecontent, bytes)
		self.assertEqual(frappe.local.response.filecontent, on_disk)
		self.assertTrue(on_disk.startswith(b"\x89PNG"))

	def test_video_audio_commands_and_sent_files_follow_the_chat(self):
		from whatsapp_connector.api import chat
		from whatsapp_connector.purge import purge_account_data

		device_id, account_id = self.approved_device()
		new_file = lambda name, content: frappe.get_doc(
			{"doctype": "File", "file_name": f"{new_id()[:6]}-{name}", "is_private": 1, "content": content}
		).insert(ignore_permissions=True)
		video = messaging.queue_message("9876501234", "watch this", attachment=new_file("clip.mp4", b"\x00\x00\x00\x18ftypmp42").file_url, account=account_id)
		audio = messaging.queue_message("9876501234", None, attachment=new_file("note.ogg", b"OggS\x00voice").file_url, account=account_id)
		captioned = messaging.queue_message("9876501234", "listen", attachment=new_file("song.mp3", b"ID3\x03tune").file_url, account=account_id)
		self.assertEqual((video.command, video.message_type), ("SEND_VIDEO", "video"))
		self.assertEqual((audio.command, audio.message_type), ("SEND_AUDIO", "audio"))
		self.assertEqual(captioned.command, "SEND_DOCUMENT")  # WhatsApp audio cannot carry a caption

		# A file uploaded for the message is attached to it...
		attached = frappe.db.get_value("File", {"file_url": video.media_file}, ["attached_to_doctype", "attached_to_name"], as_dict=True)
		self.assertEqual((attached.attached_to_doctype, attached.attached_to_name), ("WhatsApp Message", video.name))

		# ...older desktop apps get video/audio as documents; 0.8.1+ natively.
		frappe.set_user(DEVICE_USER)
		commands = {j["job_id"]: j["command"] for j in api.claim_jobs(device_id)["jobs"]}
		self.assertEqual((commands[video.name], commands[audio.name]), ("SEND_DOCUMENT", "SEND_DOCUMENT"))
		frappe.set_user("Administrator")
		frappe.db.set_value("WhatsApp Device", device_id, "app_version", "0.8.1")
		job = frappe.get_doc("WhatsApp Message", video.name)
		self.assertEqual(api._job_payload(device_id, job)["command"], "SEND_VIDEO")

		# Missing media says why.
		frappe.set_user(DEVICE_USER)
		api.push_incoming(device_id, [
			self.incoming(account_id, "MED-OLD", message_type="image", history=True, media={"mimetype": "image/jpeg", "filename": "old.jpg"}, context={"media_skipped": "history"}),
			self.incoming(account_id, "MED-BIG", message_type="video", media={"mimetype": "video/mp4"}, context={"media_skipped": "too_large"}),
			self.incoming(account_id, "MED-NEW", message_type="image", media={"mimetype": "image/jpeg"}),
		])
		frappe.set_user("Administrator")
		notes = {m.whatsapp_message_id: m.media_note for m in chat.get_messages(account_id, "919811122233")["messages"]}
		self.assertEqual((notes["MED-OLD"], notes["MED-BIG"], notes["MED-NEW"]), ("on_request", "too_large", "pending"))

		# Logging out / unlinking deletes the sent files too.
		purge_account_data(account_id, str(now_datetime()))
		self.assertFalse(frappe.db.exists("File", {"file_url": video.media_file}))

	def test_media_download_on_request(self):
		from unittest.mock import patch

		from whatsapp_connector.api import chat

		device_id, account_id = self.approved_device()
		thumb = base64.b64encode(tiny_image("JPEG")).decode()
		frappe.set_user(DEVICE_USER)
		self.assertEqual(api.heartbeat(device_id)["config"]["media_download"], "On Request")
		api.push_incoming(device_id, [self.incoming(account_id, "OD-1", message_type="image", content="price list",
			media={"mimetype": "image/jpeg", "filename": "list.jpg", "size": 5000, "thumbnail": thumb}, context={"media_on_request": True})])
		frappe.set_user("Administrator")
		m = chat.get_messages(account_id, "919811122233")["messages"][-1]
		self.assertEqual(m.media_note, "on_request")
		self.assertTrue(m.thumbnail.startswith("data:image/jpeg;base64,"))
		self.assertNotIn("context_json", m)

		# The user clicks Download: the computer is told and sees the request.
		with patch("whatsapp_connector.utils.notify_device") as notify:
			r = chat.request_media(m.name)
		self.assertTrue(r["requested"] and r["computer_online"])
		notify.assert_called()
		self.assertEqual(chat.get_messages(account_id, "919811122233")["messages"][-1].media_note, "requested")
		frappe.set_user(DEVICE_USER)
		reqs = api.claim_media_requests(device_id)["requests"]
		self.assertEqual([(q["account_id"], q["message_id"], q["filename"]) for q in reqs], [(account_id, "OD-1", "list.jpg")])

		# Failure is shown and can be retried; a successful upload ends the request.
		api.report_media_failed(device_id, account_id, "OD-1", "Media expired and not on the phone")
		frappe.set_user("Administrator")
		m = chat.get_messages(account_id, "919811122233")["messages"][-1]
		self.assertEqual((m.media_note, m.media_error), ("failed", "Media expired and not on the phone"))
		chat.request_media(m.name)
		frappe.set_user(DEVICE_USER)
		api.upload_incoming_media(device_id, account_id, "OD-1", "list.jpg", base64.b64encode(tiny_image("JPEG")).decode(), "image/jpeg")
		self.assertEqual(api.claim_media_requests(device_id)["requests"], [])
		frappe.set_user("Administrator")
		m = chat.get_messages(account_id, "919811122233")["messages"][-1]
		self.assertTrue(m.media_file and m.media_note is None)

		# Other users cannot request someone else's media.
		frappe.set_user(NO_ROLE_USER)
		with self.assertRaises(frappe.PermissionError):
			chat.request_media(m.name)

	def test_conflicting_requests_are_retried(self):
		from unittest.mock import patch

		from whatsapp_connector.api import chat
		from whatsapp_connector.utils import retry_on_conflict

		calls = []

		@retry_on_conflict
		def flaky():
			calls.append(1)
			if len(calls) < 3:
				raise frappe.QueryDeadlockError("1020 Record has changed since last read")
			return "ok"

		with patch("frappe.db.rollback"):
			self.assertEqual(flaky(), "ok")
		self.assertEqual(len(calls), 3)

		@retry_on_conflict(attempts=2)
		def always():
			raise frappe.QueryDeadlockError("conflict")

		with patch("frappe.db.rollback"), self.assertRaises(frappe.QueryDeadlockError):
			always()

		# Opening a chat never shows an error, even if the conflict persists.
		device_id, account_id = self.approved_device()
		with patch("whatsapp_connector.api.chat._mark_read", side_effect=frappe.QueryDeadlockError("x")), patch("frappe.db.rollback"):
			self.assertEqual(chat.mark_read(account_id, "919811122233"), {"ok": False})

	def test_messages_show_sender_and_receiver_numbers(self):
		device_id, account_id = self.approved_device(phone="+91 98765 43210")  # our WhatsApp number
		own = "919876543210"
		frappe.set_user(DEVICE_USER)
		lid = "998877665544332@lid"
		api.push_incoming(device_id, [
			self.incoming(account_id, "NUM-IN"),  # from 919811122233
			self.incoming(account_id, "NUM-PHONE", from_me=True, content="typed on the phone"),
			self.incoming(account_id, "NUM-GRP", chat_jid="120363000000000001@g.us", is_group=True, sender_jid="919811100001@s.whatsapp.net", sender_phone="919811100001"),
			self.incoming(account_id, "NUM-LID", chat_jid=lid, sender_jid=lid, sender_phone=None),
			self.incoming(account_id, "NUM-HIST", history=True),
		])
		frappe.set_user("Administrator")
		out = messaging.queue_message("98111 22233", "reply", account=account_id)
		nums = lambda mid: frappe.db.get_value("WhatsApp Message", {"account": account_id, "whatsapp_message_id": mid}, ["from_number", "to_number"])
		self.assertEqual(nums("NUM-IN"), ("919811122233", own))
		self.assertEqual(nums("NUM-HIST"), ("919811122233", own))  # bulk history path too
		self.assertEqual(nums("NUM-PHONE"), (own, "919811122233"))
		self.assertEqual(nums("NUM-GRP"), ("919811100001", "120363000000000001@g.us"))
		self.assertEqual((out.from_number, out.to_number), (own, "919811122233"))
		self.assertEqual(nums("NUM-LID"), (None, own))  # privacy ID: sender number not known yet

		# The contact's number behind the privacy ID becomes known later.
		from whatsapp_connector.contacts import merge_lid_messages

		merge_lid_messages(account_id, lid, "919800000099")
		self.assertEqual(nums("NUM-LID"), ("919800000099", own))

	def test_new_chat_contact_search(self):
		from whatsapp_connector.api import chat
		from whatsapp_connector.contacts import upsert_contact

		device_id, account_id = self.approved_device()
		upsert_contact(account_id, {"jid": "919811100011@s.whatsapp.net", "name": "Ramesh Traders"})
		upsert_contact(account_id, {"jid": "120363000000000099@g.us", "name": "Sales Team", "is_group": True})
		upsert_contact(account_id, {"jid": "555000555000777@lid", "notify": "Hidden Number"})
		by_name = chat.search_contacts("ramesh", account_id)
		self.assertEqual([(r["value"], r["label"]) for r in by_name], [("919811100011", "Ramesh Traders")])
		self.assertEqual([r["value"] for r in chat.search_contacts("98111 000", account_id)], ["919811100011"])  # by digits
		everyone = {r["value"]: r for r in chat.search_contacts("", account_id)}
		self.assertEqual(everyone["120363000000000099@g.us"]["label"], "Sales Team")
		self.assertIn("555000555000777@lid", everyone)  # opened by chat ID
		self.assertEqual(chat.search_contacts("ramesh", None), [])
		frappe.set_user(NO_ROLE_USER)
		with self.assertRaises(frappe.PermissionError):
			chat.search_contacts("ramesh", account_id)

	def test_whatsapp_sent_automatically_for_new_documents(self):
		from unittest.mock import patch

		from PIL import Image

		from whatsapp_connector.api import document_send

		device_id, account_id = self.approved_device()
		frappe.db.set_value("WhatsApp Account", account_id, "user", "Administrator")  # the user saving the documents
		document_send.ensure_default_doctypes()
		rules = {r.document_type: r.send_on for r in frappe.get_single("WhatsApp Connector Settings").send_doctypes}
		self.assertEqual((rules.get("Sales Invoice"), rules.get("Customer")), ("Submit", "New Document"))

		def new_customer(**extra):
			return frappe.get_doc({"doctype": "Customer", "customer_name": f"WA Auto {new_id()[:6]}", "customer_type": "Company", **extra}).insert(
				ignore_permissions=True, ignore_mandatory=True
			)

		def comments(doc):
			return frappe.get_all("Comment", filters={"reference_doctype": doc.doctype, "reference_name": doc.name}, pluck="content")

		customer = new_customer(mobile_no="+91 98111 00066")
		msg = frappe.get_doc("WhatsApp Message", {"reference_doctype": "Customer", "reference_name": customer.name})
		self.assertEqual((msg.phone, msg.account, msg.status, msg.command), ("919811100066", account_id, "Queued", "SEND_TEXT"))
		self.assertTrue(msg.content.startswith(f"Dear {customer.customer_name},"))
		self.assertTrue(any("queued to +919811100066" in c for c in comments(customer)))

		# Never twice for the same event.
		self.assertEqual(document_send.send_for_document("Customer", customer.name, "New Document", "Administrator"), msg.name)
		self.assertEqual(frappe.db.count("WhatsApp Message", {"reference_name": customer.name}), 1)

		# No mobile number: nothing sent, the reason is noted on the document.
		silent = new_customer()
		self.assertFalse(frappe.db.exists("WhatsApp Message", {"reference_name": silent.name}))
		self.assertTrue(any("no mobile number" in c for c in comments(silent)))

		# With the PDF (kept on the document).
		settings = frappe.get_single("WhatsApp Connector Settings")
		for row in settings.send_doctypes:
			if row.document_type == "Customer":
				row.attach_pdf = 1
		settings.save()
		buffer = io.BytesIO()
		Image.new("RGB", (20, 20), (255, 255, 255)).save(buffer, format="PDF")
		with patch("frappe.get_print", return_value=buffer.getvalue()):
			with_pdf = new_customer(mobile_no="9811100077")
		msg = frappe.get_doc("WhatsApp Message", {"reference_name": with_pdf.name})
		self.assertEqual(msg.command, "SEND_PDF")
		self.assertTrue(frappe.db.exists("File", {"attached_to_doctype": "Customer", "attached_to_name": with_pdf.name}))

		# DocTypes not in the list send nothing.
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("WhatsApp Message", {"reference_name": todo.name}))

	def test_new_whatsapp_message_from_the_form(self):
		from unittest.mock import patch

		from whatsapp_connector.contacts import upsert_contact

		sender = "wa-compose-user@example.com"
		make_user(sender, ["WhatsApp User"])
		device_id, account_id = self.approved_device()
		_, other_account = self.approved_device()
		frappe.db.set_value("WhatsApp Account", account_id, "user", sender)
		upsert_contact(account_id, {"jid": "919811100088@s.whatsapp.net", "name": "Form Customer"})
		upsert_contact(account_id, {"jid": "120363000000000077@g.us", "name": "Form Group", "is_group": True})
		person = frappe.db.get_value("WhatsApp Contact", {"account": account_id, "jid": "919811100088@s.whatsapp.net"})
		group = frappe.db.get_value("WhatsApp Contact", {"account": account_id, "jid": "120363000000000077@g.us"})
		new = lambda **v: frappe.get_doc({"doctype": "WhatsApp Message", **v}).insert()  # with permission checks

		frappe.set_user(sender)
		with patch("whatsapp_connector.utils.notify_device") as notify:
			typed = new(account=account_id, phone="98111 22233", content="Hello from the form")
		self.assertEqual(
			(typed.direction, typed.status, typed.command, typed.phone, typed.chat_jid, typed.device),
			("Outgoing", "Queued", "SEND_TEXT", "919811122233", "919811122233@s.whatsapp.net", device_id),
		)
		notify.assert_called_with(device_id)  # the computer is told at once
		picked = new(account=account_id, send_to_contact=person, content="Hi")
		self.assertEqual(picked.phone, "919811100088")
		to_group = new(account=account_id, send_to_contact=group, content="Hello team")
		self.assertEqual((to_group.chat_jid, to_group.is_group), ("120363000000000077@g.us", 1))

		# With a file.
		f = frappe.get_doc({"doctype": "File", "file_name": f"q-{new_id()[:6]}.png", "is_private": 1, "content": tiny_image("PNG")}).insert(ignore_permissions=True)
		pic = new(account=account_id, phone="9811122233", media_file=f.file_url)
		self.assertEqual((pic.command, pic.message_type), ("SEND_IMAGE", "image"))

		# Once saved, a message is a record: the sender cannot change it.
		typed.reload()
		typed.content = "edited"
		with self.assertRaises(frappe.ValidationError):
			typed.save()

		# Guard rails.
		with self.assertRaises(frappe.ValidationError):
			new(account=account_id, content="no number")
		with self.assertRaises(frappe.ValidationError):
			new(account=account_id, phone="9811122233")  # nothing to send
		with self.assertRaises(frappe.PermissionError):
			new(account=other_account, phone="9811122233", content="not my number")
		frappe.set_user("Administrator")
		frappe.db.set_value("WhatsApp Account", account_id, "status", "logged_out")
		frappe.set_user(sender)
		with self.assertRaises(frappe.ValidationError):
			new(account=account_id, phone="9811122233", content="unlinked")
		frappe.set_user("Administrator")

	def test_send_endpoint_requires_sender_role(self):
		self.approved_device()
		frappe.set_user(DEVICE_USER)  # device integration users cannot queue messages
		with self.assertRaises(frappe.PermissionError):
			messaging.send("9876501234", "nope")

	def test_offline_detection(self):
		device_id, account_id = self.approved_device()
		frappe.db.set_value("WhatsApp Device", device_id, "last_heartbeat", add_to_date(now_datetime(), minutes=-10))
		tasks.mark_offline_devices()
		self.assertEqual(frappe.db.get_value("WhatsApp Device", device_id, "connection_status"), "Offline")
		self.assertEqual(frappe.db.get_value("WhatsApp Account", account_id, "status"), "unknown")

	def test_disabled_connector_refuses_devices(self):
		device_id, _ = self.approved_device()
		frappe.db.set_single_value("WhatsApp Connector Settings", "enabled", 0)
		frappe.clear_document_cache("WhatsApp Connector Settings", "WhatsApp Connector Settings")
		try:
			frappe.set_user(DEVICE_USER)
			with self.assertRaises(api.ConnectorDisabled):
				api.heartbeat(device_id)
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_single_value("WhatsApp Connector Settings", "enabled", 1)
			frappe.clear_document_cache("WhatsApp Connector Settings", "WhatsApp Connector Settings")


	# ------------------------------------------------------------ chat & reactions

	def test_send_to_group_and_lid_chats(self):
		device_id, account_id = self.approved_device()
		group = messaging.queue_message(chat_jid="120363012345678901@g.us", message="hello group", account=account_id)
		self.assertEqual((group.phone, group.is_group, group.chat_jid), (None, 1, "120363012345678901@g.us"))
		lid = messaging.queue_message(chat_jid="123456789012345@lid", message="hi", account=account_id)
		self.assertIsNone(lid.phone)
		plain = messaging.queue_message("9876501234", "hi", account=account_id)
		self.assertEqual(plain.chat_jid, "919876501234@s.whatsapp.net")
		with self.assertRaises(frappe.ValidationError):
			messaging.queue_message(chat_jid="evil@example.com", message="x", account=account_id)

		frappe.set_user(DEVICE_USER)
		jobs = {j["job_id"]: j for j in api.claim_jobs(device_id)["jobs"]}
		self.assertEqual(jobs[group.name]["chat_jid"], "120363012345678901@g.us")
		self.assertIsNone(jobs[group.name]["to"])
		self.assertEqual(jobs[plain.name]["chat_jid"], "919876501234@s.whatsapp.net")

	def test_reactions(self):
		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		pushed = api.push_incoming(device_id, [self.incoming(account_id, "WA-IN-REACT")])
		target = pushed["accepted"][0]["name"]
		frappe.set_user("Administrator")

		reaction = messaging.queue_reaction(target, "👍")
		self.assertEqual((reaction.command, reaction.message_type, reaction.quoted_message_id), ("SEND_REACTION", "reaction", "WA-IN-REACT"))
		frappe.set_user(DEVICE_USER)
		job = next(j for j in api.claim_jobs(device_id)["jobs"] if j["job_id"] == reaction.name)
		self.assertEqual(job["reaction"], {"message_id": "WA-IN-REACT", "from_me": False, "participant": None})
		self.assertEqual(job["chat_jid"], "919811122233@s.whatsapp.net")
		self.assertEqual(job["content"], "👍")
		frappe.set_user("Administrator")

		with self.assertRaises(frappe.ValidationError):
			messaging.queue_reaction(reaction.name, "👍")  # reacting to a reaction
		with self.assertRaises(frappe.ValidationError):
			messaging.queue_reaction(target, "nice")  # not an emoji
		unsent = messaging.queue_message("9876501234", "not sent yet", account=account_id)
		with self.assertRaises(frappe.ValidationError):
			messaging.queue_reaction(unsent.name, "❤️")  # no WhatsApp ID yet
		self.assertEqual(messaging.queue_reaction(target, "").content or "", "")  # removal allowed

	def test_phone_and_privacy_id_are_one_conversation(self):
		"""WhatsApp may address one person by @lid in incoming messages and by phone
		number when we send; both must land in a single conversation."""
		from whatsapp_connector.api import chat
		from whatsapp_connector.patches.v1 import set_conversation_key

		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		api.push_incoming(
			device_id,
			[self.incoming(account_id, "LID-1", chat_jid="130373849735223@lid", sender_jid="130373849735223@lid", sender_phone="919625441390", sender_name="Naman")],
		)
		frappe.set_user("Administrator")
		messaging.queue_message("96254 41390", "hello", account=account_id)
		convs = [c for c in chat.get_conversations(account=account_id) if c.conversation == "919625441390"]
		self.assertEqual(len(convs), 1)
		self.assertEqual(convs[0].title, "Naman")
		self.assertEqual(len(chat.get_messages(account_id, "919625441390")["messages"]), 2)

		# @lid chat whose phone number is only learned later
		lid = "555555555555555@lid"
		frappe.set_user(DEVICE_USER)
		api.push_incoming(device_id, [self.incoming(account_id, "LID-2", chat_jid=lid, sender_jid=lid, sender_phone=None)])
		frappe.set_user("Administrator")
		reply = messaging.queue_message(chat_jid=lid, message="reply before phone known", account=account_id)
		self.assertEqual(reply.conversation_key, lid)
		self.assertEqual(chat.get_messages(account_id, lid)["send_to"], lid)
		frappe.set_user(DEVICE_USER)
		api.push_incoming(device_id, [self.incoming(account_id, "LID-3", chat_jid=lid, sender_jid=lid, sender_phone="919000000123")])
		frappe.set_user("Administrator")
		thread = chat.get_messages(account_id, "919000000123")
		self.assertEqual(len(thread["messages"]), 3)  # earlier @lid rows merged
		self.assertEqual(thread["send_to"], "919000000123@s.whatsapp.net")
		self.assertFalse([c for c in chat.get_conversations(account=account_id) if c.conversation == lid])

		# Backfill patch reproduces the keys for existing rows
		frappe.db.sql("update `tabWhatsApp Message` set conversation_key = null where account = %s", account_id)
		set_conversation_key.execute()
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account_id, "conversation_key": "919000000123"}), 3)
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account_id, "conversation_key": "919625441390"}), 2)

	def test_contact_sync_merges_identities(self):
		from whatsapp_connector.api import chat

		device_id, account_id = self.approved_device()
		lid = "777777777777777@lid"
		frappe.set_user(DEVICE_USER)
		api.push_incoming(device_id, [self.incoming(account_id, "CT-1", chat_jid=lid, sender_jid=lid, sender_phone=None, sender_name=None)])
		self.assertEqual(api.push_contacts(device_id, [{"account_id": account_id, "jid": lid, "notify": "Ravi P"}])["upserted"], 1)
		# later the privacy-ID <-> phone mapping arrives, with the address-book name
		api.push_contacts(device_id, [{"account_id": account_id, "jid": "919812345678@s.whatsapp.net", "lid": lid, "name": "Ravi (Supplier)"}])
		result = api.push_contacts(
			device_id,
			[{"account_id": account_id, "jid": "120363000000000001@g.us", "name": "Purchase Team"}, {"account_id": new_id(), "jid": "1@lid"}],
		)
		self.assertEqual((result["upserted"], result["skipped"]), (1, 1))  # other device's account ignored
		frappe.set_user("Administrator")

		people = frappe.get_all("WhatsApp Contact", filters={"account": account_id, "is_group": 0}, fields=["jid", "lid", "phone", "display_name", "push_name"])
		self.assertEqual(len(people), 1)  # merged into one record
		p = people[0]
		self.assertEqual((p.jid, p.phone, p.lid, p.display_name, p.push_name), ("919812345678@s.whatsapp.net", "919812345678", lid, "Ravi (Supplier)", "Ravi P"))

		msg = frappe.db.get_value("WhatsApp Message", {"whatsapp_message_id": "CT-1", "account": account_id}, ["conversation_key", "phone"], as_dict=True)
		self.assertEqual((msg.conversation_key, msg.phone), ("919812345678", "919812345678"))

		frappe.set_user(DEVICE_USER)
		api.push_incoming(
			device_id,
			[
				self.incoming(account_id, "CT-2", chat_jid=lid, sender_jid=lid, sender_phone=None),
				self.incoming(account_id, "CT-G", chat_jid="120363000000000001@g.us", is_group=True),
			],
		)
		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.get_value("WhatsApp Message", {"whatsapp_message_id": "CT-2", "account": account_id}, "conversation_key"), "919812345678"
		)
		titles = {c.conversation: c.title for c in chat.get_conversations(account=account_id)}
		self.assertEqual(titles["919812345678"], "Ravi (Supplier)")
		self.assertEqual(titles["120363000000000001@g.us"], "Purchase Team")

	def test_contact_sync_never_touches_erpnext_contacts(self):
		device_id, account_id = self.approved_device()
		existing = frappe.get_doc(
			{"doctype": "Contact", "first_name": "Existing WA", "phone_nos": [{"phone": "+91 98123-00001", "is_primary_mobile_no": 1}]}
		).insert(ignore_permissions=True)
		contacts_before = frappe.db.count("Contact")
		frappe.set_user(DEVICE_USER)
		api.push_contacts(
			device_id,
			[
				{"account_id": account_id, "jid": "919812300001@s.whatsapp.net", "notify": "X"},
				{"account_id": account_id, "jid": "919812300002@s.whatsapp.net", "name": "New Person"},
			],
		)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.count("Contact"), contacts_before)
		self.assertEqual(str(frappe.db.get_value("Contact", existing.name, "modified")), str(existing.modified))
		self.assertFalse(frappe.db.get_value("WhatsApp Contact", {"account": account_id, "phone": "919812300001"}, "contact"))

	def test_unlinking_keeps_chats_and_contacts(self):
		device_id, account_id = self.approved_device()
		_, other_account = self.approved_device()
		frappe.set_user(DEVICE_USER)
		api.push_incoming(device_id, [self.incoming(account_id, "P-1"), self.incoming(account_id, "P-IMG", message_type="image")])
		api.upload_incoming_media(device_id, account_id, "P-IMG", "p.jpg", base64.b64encode(tiny_image("JPEG")).decode(), "image/jpeg")
		api.push_contacts(device_id, [{"account_id": account_id, "jid": "919812399999@s.whatsapp.net", "name": "Stays"}])
		frappe.set_user("Administrator")
		file_url = frappe.db.get_value("WhatsApp Message", {"account": account_id, "whatsapp_message_id": "P-IMG"}, "media_file")

		# Logged out from the phone / unlinked in the app (also "remove account").
		frappe.set_user(DEVICE_USER)
		api.purge_account(device_id, account_id)
		api.purge_account(device_id, account_id, remove_account=1)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account_id}), 2)
		self.assertEqual(frappe.db.count("WhatsApp Contact", {"account": account_id}), 1)
		self.assertTrue(frappe.db.exists("File", {"file_url": file_url}))
		self.assertEqual(frappe.db.get_value("WhatsApp Account", account_id, "status"), "logged_out")
		# The computer going offline / quitting never turns an unlinked number back into a usable one.
		frappe.db.set_value("WhatsApp Device", device_id, "last_heartbeat", add_to_date(now_datetime(), hours=-1))
		tasks.mark_offline_devices()
		frappe.set_user(DEVICE_USER)
		api.heartbeat(device_id, going_offline=1)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("WhatsApp Account", account_id, "status"), "logged_out")

		# Kept for reference, but nothing can be sent through an unlinked WhatsApp.
		with self.assertRaises(frappe.ValidationError):
			messaging.queue_message("9876501234", "hello", account=account_id)
		msg = frappe.db.get_value("WhatsApp Message", {"account": account_id, "whatsapp_message_id": "P-1"})
		with self.assertRaises(frappe.ValidationError):
			messaging.queue_reaction(msg, "👍")

		# A device can only mark its own accounts.
		frappe.set_user(DEVICE_USER)
		with self.assertRaises(frappe.PermissionError):
			api.purge_account(device_id, other_account)

		# A QR code started but never scanned leaves nothing behind.
		empty = new_id()
		api.heartbeat(device_id, accounts=[{"account_id": empty, "status": "qr"}])
		api.purge_account(device_id, empty, remove_account=1)
		self.assertFalse(frappe.db.exists("WhatsApp Account", empty))
		# ...even when a last status update arrives after it was dropped.
		api.heartbeat(device_id, accounts=[{"account_id": empty, "status": "logged_out"}])
		self.assertFalse(frappe.db.exists("WhatsApp Account", empty))

	def test_history_import_is_read_and_ordered_by_whatsapp_time(self):
		from whatsapp_connector.api import chat

		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		api.push_incoming(device_id, [self.incoming(account_id, "H-NEW", timestamp=1763700500, content="newer")])
		api.push_incoming(device_id, [self.incoming(account_id, "H-OLD", timestamp=1763600000, content="older (history)", history=True)])
		frappe.set_user("Administrator")
		thread = chat.get_messages(account_id, "919811122233")
		self.assertEqual([m.whatsapp_message_id for m in thread["messages"]], ["H-OLD", "H-NEW"])  # WhatsApp time, not arrival
		conv = next(c for c in chat.get_conversations(account=account_id) if c.conversation == "919811122233")
		self.assertEqual(conv.unread, 1)  # history is not unread
		self.assertEqual(conv.last.content, "newer")
		older = chat.get_messages(account_id, "919811122233", before=thread["messages"][-1].sort_at)
		self.assertEqual([m.whatsapp_message_id for m in older["messages"]], ["H-OLD"])

	def test_history_bulk_insert(self):
		import time

		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		lid = "77766655544433@lid"
		batch = [
			self.incoming(account_id, f"BULK-{i}", timestamp=1763600000 + i, content=f"old {i}", history=True)
			for i in range(100)
		]
		batch[5] = self.incoming(account_id, "BULK-LID", chat_jid=lid, sender_jid=lid, sender_phone=None, history=True)
		started = time.monotonic()
		first = api.push_incoming(device_id, batch)
		elapsed = time.monotonic() - started
		self.assertEqual(len(first["accepted"]), 100)
		self.assertLess(elapsed, 5)

		# Re-sent batch (desktop retry) plus a duplicate inside the batch: no new rows.
		again = api.push_incoming(device_id, batch[:10] + [batch[0]])
		self.assertEqual(len(again["accepted"]), 0)
		self.assertEqual(len(again["duplicates"]), 11)
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account_id, "whatsapp_message_id": ("like", "BULK-%")}), 100)

		row = frappe.db.get_value(
			"WhatsApp Message",
			{"account": account_id, "whatsapp_message_id": "BULK-7"},
			["direction", "status", "is_read", "conversation_key", "owner", "message_type", "attempts", "message_timestamp"],
			as_dict=True,
		)
		self.assertEqual((row.direction, row.status, row.is_read, row.conversation_key), ("Incoming", "Received", 1, "919811122233"))
		self.assertEqual((row.owner, row.message_type, row.attempts), (DEVICE_USER, "text", 0))
		self.assertIsNotNone(row.message_timestamp)

		# The phone of a privacy-ID chat moves its earlier messages to the phone conversation.
		api.push_incoming(device_id, [self.incoming(account_id, "BULK-LID-2", chat_jid=lid, sender_phone="919800000077", history=True)])
		keys = frappe.get_all("WhatsApp Message", {"account": account_id, "chat_jid": lid}, pluck="conversation_key")
		self.assertEqual(set(keys), {"919800000077"})

		# A bad item does not lose the batch: it is rejected on its own.
		mixed = api.push_incoming(
			device_id,
			[self.incoming(account_id, "BULK-OK", history=True), {**self.incoming(account_id, "", history=True)}],
		)
		self.assertEqual([a["message_id"] for a in mixed["accepted"]], ["BULK-OK"])
		self.assertEqual(len(mixed["rejected"]), 1)
		frappe.set_user("Administrator")

	def test_users_see_only_their_own_chats_and_contacts(self):
		from unittest.mock import patch

		from whatsapp_connector import access
		from whatsapp_connector.api import chat
		from whatsapp_connector.contacts import upsert_contact

		sudhir, varsha, manager = "wa-own-sudhir@example.com", "wa-own-varsha@example.com", "wa-own-manager@example.com"
		make_user(sudhir, ["WhatsApp User"])
		make_user(varsha, ["WhatsApp User"])
		make_user(manager, ["WhatsApp Manager"])
		dev_s, acc_s = self.approved_device(phone="+91 90000 00001")
		dev_v, acc_v = self.approved_device(phone="+91 90000 00002")
		frappe.db.set_value("WhatsApp Device", dev_s, "assigned_user", sudhir)
		frappe.db.set_value("WhatsApp Device", dev_v, "assigned_user", varsha)
		frappe.db.set_value("WhatsApp Account", acc_s, "user", sudhir)  # owner (set when linked)
		frappe.db.set_value("WhatsApp Account", acc_v, "user", varsha)

		frappe.set_user(DEVICE_USER)
		api.push_incoming(dev_s, [self.incoming(acc_s, "OWN-S1", content="for sudhir")])
		api.push_incoming(dev_v, [self.incoming(acc_v, "OWN-V1", content="for varsha", chat_jid="919822200000@s.whatsapp.net", sender_phone="919822200000")])
		frappe.set_user("Administrator")
		upsert_contact(acc_s, {"jid": "919811122233@s.whatsapp.net", "name": "Sudhir's customer"})
		upsert_contact(acc_v, {"jid": "919822200000@s.whatsapp.net", "name": "Varsha's customer"})
		msg_s = frappe.db.get_value("WhatsApp Message", {"account": acc_s, "whatsapp_message_id": "OWN-S1"})
		msg_v = frappe.db.get_value("WhatsApp Message", {"account": acc_v, "whatsapp_message_id": "OWN-V1"})

		frappe.set_user(sudhir)
		self.assertEqual([a.name for a in chat.get_accounts()], [acc_s])
		self.assertEqual({c.account for c in chat.get_conversations()}, {acc_s})
		self.assertEqual(len(chat.get_messages(acc_s, "919811122233")["messages"]), 1)
		with self.assertRaises(frappe.PermissionError):
			chat.get_messages(acc_v, "919822200000")
		with self.assertRaises(frappe.PermissionError):
			chat.get_conversations(account=acc_v)
		with self.assertRaises(frappe.PermissionError):
			chat.mark_read(acc_v, "919822200000")
		# List views / REST / reports
		self.assertEqual(set(frappe.get_list("WhatsApp Message", filters={"account": ("in", [acc_s, acc_v])}, pluck="account")), {acc_s})
		self.assertEqual(set(frappe.get_list("WhatsApp Contact", filters={"account": ("in", [acc_s, acc_v])}, pluck="account")), {acc_s})
		self.assertEqual(set(frappe.get_list("WhatsApp Account", filters={"name": ("in", [acc_s, acc_v])}, pluck="name")), {acc_s})
		self.assertTrue(frappe.has_permission("WhatsApp Message", "read", msg_s))
		self.assertFalse(frappe.has_permission("WhatsApp Message", "read", msg_v))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("WhatsApp Message", msg_v).check_permission("read")
		# Sending / reacting only through their own number
		with self.assertRaises(frappe.PermissionError):
			messaging.send(to="98222 00000", message="hi", account=acc_v)
		with self.assertRaises(frappe.PermissionError):
			messaging.react(msg_v, "👍")
		self.assertEqual(frappe.db.get_value("WhatsApp Message", messaging.send(to="98111 22233", message="hi")["name"], "account"), acc_s)

		frappe.set_user(varsha)
		self.assertEqual({c.account for c in chat.get_conversations()}, {acc_v})
		self.assertFalse(frappe.has_permission("WhatsApp Message", "read", msg_s))

		frappe.set_user(NO_ROLE_USER)
		with self.assertRaises(frappe.PermissionError):
			chat.get_conversations()

		frappe.set_user(manager)
		# (Per account: a busy site's first page of chats may be all real conversations.)
		self.assertTrue(chat.get_conversations(account=acc_s) and chat.get_conversations(account=acc_v))
		self.assertTrue(frappe.has_permission("WhatsApp Message", "read", msg_v))

		# Realtime events go only to the owner and managers.
		frappe.set_user("Administrator")
		recipients = access.users_for_account(acc_s)
		self.assertIn(sudhir, recipients)
		self.assertIn(manager, recipients)
		self.assertNotIn(varsha, recipients)
		with patch("frappe.publish_realtime") as pub:
			access.publish_chat_event(acc_v, {"account": acc_v})
		users = {c.kwargs["user"] for c in pub.call_args_list}
		self.assertIn(varsha, users)
		self.assertNotIn(sudhir, users)

		# A WhatsApp without an owner is visible to managers only.
		frappe.db.set_value("WhatsApp Account", acc_v, "user", None)
		frappe.set_user(varsha)
		self.assertEqual(chat.get_accounts(), [])
		self.assertEqual(chat.get_conversations(), [])
		frappe.set_user("Administrator")

	def test_chat_api(self):
		from whatsapp_connector.api import chat

		device_id, account_id = self.approved_device()
		frappe.set_user(DEVICE_USER)
		api.push_incoming(
			device_id,
			[
				self.incoming(account_id, "CHAT-1", content="first"),
				self.incoming(account_id, "CHAT-2", content="second"),
				# the contact reacts to their own first message, then changes it
				self.incoming(account_id, "CHAT-R1", message_type="reaction", content="😂", quoted_message_id="CHAT-1"),
				self.incoming(account_id, "CHAT-R2", message_type="reaction", content="❤️", quoted_message_id="CHAT-1"),
			],
		)
		frappe.set_user("Administrator")
		first = frappe.db.get_value("WhatsApp Message", {"whatsapp_message_id": "CHAT-1", "account": account_id})
		messaging.queue_reaction(first, "👍")

		convs = [c for c in chat.get_conversations(account=account_id) if c.conversation == "919811122233"]
		self.assertEqual(len(convs), 1)
		self.assertEqual(convs[0].unread, 2)  # reactions are not counted
		self.assertEqual(convs[0].title, "Customer")

		thread = chat.get_messages(account_id, "919811122233")
		self.assertEqual(thread["send_to"], "919811122233@s.whatsapp.net")
		self.assertEqual([m.whatsapp_message_id for m in thread["messages"]], ["CHAT-1", "CHAT-2"])
		reactions = {r["emoji"]: r for r in thread["messages"][0].reactions}
		self.assertEqual(set(reactions), {"❤️", "👍"})  # contact's latest reaction replaced 😂
		self.assertTrue(reactions["👍"]["mine"] and reactions["👍"]["pending"])

		chat.mark_read(account_id, "919811122233")
		convs = [c for c in chat.get_conversations(account=account_id) if c.conversation == "919811122233"]
		self.assertEqual(convs[0].unread, 0)
		self.assertEqual(chat.get_conversations(account=account_id, search="no-such-thing"), [])
		self.assertEqual(chat.resolve_chat("98765 43210")["chat_jid"], "919876543210@s.whatsapp.net")

		frappe.set_user(DEVICE_USER)
		with self.assertRaises(frappe.PermissionError):
			chat.get_conversations()
		with self.assertRaises(frappe.PermissionError):
			messaging.react(first, "👍")


class TestUserLogin(IntegrationTestCase):
	"""Desktop login with an ERPNext user's email + password; WhatsApp Connection User."""

	PASSWORD = "Wac-Test-Pass-2026!"
	ALICE = "wa-login-alice@example.com"
	BOB = "wa-login-bob@example.com"
	NOROLE = "wa-login-norole@example.com"
	MANAGER = "wa-login-manager@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(DEVICE_USER, ["WhatsApp Device"])
		for email, roles in ((cls.ALICE, ["WhatsApp User"]), (cls.BOB, ["WhatsApp User"]), (cls.NOROLE, []), (cls.MANAGER, ["WhatsApp Manager"])):
			make_user(email, roles)
			from frappe.utils.password import update_password

			update_password(email, cls.PASSWORD)
		settings = frappe.get_single("WhatsApp Connector Settings")
		settings.update({"enabled": 1})
		settings.save()

	def setUp(self):
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.local.whatsapp_device = None
		frappe.set_user("Administrator")

	def login(self, user=None, device_id=None, pwd=None):
		from unittest.mock import patch

		frappe.local.request_ip = "127.0.0.1"  # Frappe's login-attempt tracker keys on the caller's IP
		frappe.set_user("Guest")
		# A failed login commits its log in Frappe: never let it commit test data.
		with patch("frappe.db.commit"):
			result = login_api._login(user or self.ALICE, pwd or self.PASSWORD, device_id or new_id(), device_name="PC", hostname="pc", platform="win32")
		frappe.set_user("Administrator")
		return result

	def act_as(self, device_id):
		"""What the auth hook does for a valid 'WhatsAppDevice' header."""
		frappe.set_user(SERVICE_USER)
		frappe.local.whatsapp_device = device_id

	def link_whatsapp(self, device_id, phone=None):
		# A different number per test: linking a number again merges its earlier chats.
		phone = phone or "98" + str(uuid.uuid4().int)[:8]
		account = new_id()
		self.act_as(device_id)
		api.heartbeat(device_id, accounts=[{"account_id": account, "label": "Sales", "phone_number": phone, "status": "connected"}])
		api.push_incoming(device_id, [{"account_id": account, "message_id": f"IN-{account[:8]}", "chat_jid": "919811122233@s.whatsapp.net", "sender_phone": "919811122233", "message_type": "text", "content": "hi", "timestamp": 1763700000}])
		frappe.local.whatsapp_device = None
		frappe.set_user("Administrator")
		return account

	def test_login_issues_token_and_connection_row(self):
		result = self.login(user=self.ALICE.upper())  # email is case-insensitive
		self.assertGreaterEqual(len(result["device_token"]), 40)
		self.assertEqual((result["user"], result["status"]), (self.ALICE, "Approved"))

		device = frappe.get_doc("WhatsApp Device", result["device_uuid"])
		self.assertEqual((device.user, device.assigned_user, device.auth_method), (SERVICE_USER, self.ALICE, "User Login"))
		self.assertNotEqual(device.token_hash, result["device_token"])
		self.assertTrue(verify_device_token(device.name, result["device_token"]))

		conn = frappe.get_doc("WhatsApp Connection User", self.ALICE)
		self.assertEqual((conn.status, conn.device, conn.access_token), ("Connected", device.name, result["device_token"]))
		self.assertFalse(frappe.db.sql("select 1 from `tabWhatsApp Connection User` where access_token like %s", f"%{self.PASSWORD}%"))

		# The WhatsApp number appears on the row; only one WhatsApp per computer.
		account = new_id()
		self.act_as(device.name)
		api.heartbeat(device.name, accounts=[
			{"account_id": account, "label": "Sales", "phone_number": "9876543210", "status": "connected"},
			{"account_id": new_id(), "label": "Second", "phone_number": "9876500000", "status": "connected"},
		])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("WhatsApp Connection User", self.ALICE, "whatsapp_number"), "919876543210")
		self.assertEqual(frappe.db.count("WhatsApp Account", {"device": device.name}), 1)

	def test_connection_row_shows_whatsapp_and_erpnext_status(self):
		result = self.login()
		device_id = result["device_uuid"]
		row = lambda: frappe.db.get_value("WhatsApp Connection User", self.ALICE, ["status", "whatsapp_status", "whatsapp_number"], as_dict=True)
		self.assertEqual((row().status, row().whatsapp_status), ("Connected", "Disconnected"))  # no WhatsApp yet

		account = new_id()
		beat = lambda status, accounts=None: (
			self.act_as(device_id),
			api.heartbeat(device_id, accounts=accounts if accounts is not None else [{"account_id": account, "label": "Sales", "phone_number": "9876543210", "status": status}]),
			frappe.set_user("Administrator"),
		)
		beat("qr")
		self.assertEqual(row().whatsapp_status, "Connecting")
		beat("connected")
		self.assertEqual((row().whatsapp_status, row().whatsapp_number), ("Connected", "919876543210"))
		beat("disconnected")  # user clicked Disconnect in the app
		self.assertEqual((row().status, row().whatsapp_status, row().whatsapp_number), ("Connected", "Disconnected", "919876543210"))

		# The computer stops responding: both show Disconnected; the next heartbeat restores them.
		beat("connected")
		frappe.db.set_value("WhatsApp Device", device_id, "last_heartbeat", add_to_date(now_datetime(), hours=-1))
		tasks.mark_offline_devices()
		self.assertEqual((row().status, row().whatsapp_status), ("Disconnected", "Disconnected"))
		beat("connected")
		self.assertEqual((row().status, row().whatsapp_status), ("Connected", "Connected"))

		# The app quits: shown as disconnected at once.
		self.act_as(device_id)
		self.assertTrue(api.heartbeat(device_id, going_offline=1)["offline"])
		frappe.set_user("Administrator")
		self.assertEqual((row().status, row().whatsapp_status), ("Disconnected", "Disconnected"))
		self.assertEqual(frappe.db.get_value("WhatsApp Device", device_id, "connection_status"), "Offline")
		beat("connected")
		self.assertEqual((row().status, row().whatsapp_status), ("Connected", "Connected"))

		beat(None, accounts=[])  # WhatsApp unlinked
		self.assertEqual((row().whatsapp_status, row().whatsapp_number), ("Disconnected", None))

	def test_login_with_email_when_user_id_differs(self):
		# Like Administrator: user ID "Administrator", email admin@example.com.
		frappe.db.set_value("User", self.BOB, "email", "bob.alias@example.com")
		result = self.login(user="bob.alias@example.com")
		self.assertEqual(result["user"], self.BOB)

	def test_bad_logins_are_rejected(self):
		from unittest.mock import patch

		# Frappe commits its failed-login log; keep the test transaction intact.
		with patch("frappe.db.commit"):
			with self.assertRaises(frappe.AuthenticationError):
				self.login(pwd="wrong-password")
			with self.assertRaises(frappe.AuthenticationError):
				self.login(user="nobody@example.com")
		with self.assertRaises(frappe.PermissionError):
			self.login(user=self.NOROLE)
		device_id = self.login()["device_uuid"]
		frappe.db.set_value("WhatsApp Device", device_id, "status", "Disabled")
		with self.assertRaises(login_api.DeviceDisabled):
			self.login(device_id=device_id)

	def test_one_computer_per_user(self):
		first = self.login()
		account = self.link_whatsapp(first["device_uuid"])
		second = self.login()
		self.assertIsNone(verify_device_token(first["device_uuid"], first["device_token"]))
		self.assertTrue(verify_device_token(second["device_uuid"], second["device_token"]))
		# The old computer's chats stay with Alice, marked as no longer linked.
		self.assertEqual(frappe.db.get_value("WhatsApp Account", account, ["status", "user"]), ("logged_out", self.ALICE))
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account}), 1)
		conn = frappe.get_doc("WhatsApp Connection User", self.ALICE)
		self.assertEqual((conn.device, conn.access_token), (second["device_uuid"], second["device_token"]))

	def test_relogin_on_same_computer_keeps_whatsapp(self):
		first = self.login()
		account = self.link_whatsapp(first["device_uuid"])
		again = self.login(device_id=first["device_uuid"])
		self.assertTrue(verify_device_token(again["device_uuid"], again["device_token"]))
		self.assertTrue(frappe.db.exists("WhatsApp Account", account))

	def test_other_user_on_same_computer_does_not_inherit_chats(self):
		alice = self.login()
		account = self.link_whatsapp(alice["device_uuid"])
		bob = self.login(user=self.BOB, device_id=alice["device_uuid"])
		# Alice's chats are kept and stay hers: Bob cannot see them.
		from whatsapp_connector.access import allowed_accounts

		self.assertEqual(frappe.db.get_value("WhatsApp Account", account, "user"), self.ALICE)
		self.assertNotIn(account, allowed_accounts(self.BOB))
		self.assertIn(account, allowed_accounts(self.ALICE))
		self.assertEqual(frappe.db.get_value("WhatsApp Device", bob["device_uuid"], "assigned_user"), self.BOB)
		self.assertEqual(frappe.db.get_value("WhatsApp Connection User", self.ALICE, "status"), "Disconnected")
		self.assertIsNone(verify_device_token(alice["device_uuid"], alice["device_token"]))

	def test_logout_from_app(self):
		result = self.login()
		account = self.link_whatsapp(result["device_uuid"])
		self.act_as(result["device_uuid"])
		self.assertTrue(login_api.logout(result["device_uuid"])["logged_out"])
		frappe.set_user("Administrator")
		self.assert_logged_out(result, account)

	def assert_logged_out(self, result, account, user=None):
		self.assertIsNone(verify_device_token(result["device_uuid"], result["device_token"]))
		conn = frappe.get_doc("WhatsApp Connection User", user or self.ALICE)
		self.assertEqual((conn.status, conn.whatsapp_status, conn.access_token), ("Disconnected", "Disconnected", None))
		# Chats are kept; the WhatsApp is marked as no longer linked.
		self.assertEqual(frappe.db.get_value("WhatsApp Account", account, "status"), "logged_out")
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account}), 1)

	def test_manager_removes_access_token(self):
		from unittest.mock import patch

		result = self.login()
		account = self.link_whatsapp(result["device_uuid"])
		frappe.set_user(self.BOB)
		with self.assertRaises(frappe.PermissionError):
			login_api.remove_access_token(self.ALICE)
		frappe.set_user(self.MANAGER)
		with patch("whatsapp_connector.api.login.notify_device") as notify:
			login_api.remove_access_token(self.ALICE)
		self.assertEqual(notify.call_args.args[2]["status"], "token_reset")  # the app unlinks WhatsApp
		frappe.set_user("Administrator")
		self.assert_logged_out(result, account)
		# ...and can log in again with email + password.
		again = self.login(device_id=result["device_uuid"])
		self.assertEqual(frappe.db.get_value("WhatsApp Connection User", self.ALICE, "access_token"), again["device_token"])

	def test_disabled_user_is_logged_out(self):
		result = self.login()
		account = self.link_whatsapp(result["device_uuid"])
		user = frappe.get_doc("User", self.ALICE)
		user.enabled = 0
		user.save(ignore_permissions=True)
		try:
			self.assert_logged_out(result, account)
		finally:
			frappe.db.set_value("User", self.ALICE, "enabled", 1)

	def test_deleting_connection_row_logs_out(self):
		result = self.login()
		account = self.link_whatsapp(result["device_uuid"])
		frappe.delete_doc("WhatsApp Connection User", self.ALICE, ignore_permissions=True)
		self.assertIsNone(verify_device_token(result["device_uuid"], result["device_token"]))
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": account}), 1)

	def test_relinking_same_number_continues_the_old_chats(self):
		first = self.login()
		number = "97" + str(uuid.uuid4().int)[:8]
		old = self.link_whatsapp(first["device_uuid"], phone=number)
		msg_id = f"IN-{old[:8]}"
		self.act_as(first["device_uuid"])
		login_api.logout(first["device_uuid"])  # chats kept
		frappe.set_user("Administrator")

		# Logs in again and links the same number: a new account; history comes again.
		second = self.login(device_id=first["device_uuid"])
		new = new_id()
		self.act_as(second["device_uuid"])
		api.heartbeat(second["device_uuid"], accounts=[{"account_id": new, "label": "Sales", "phone_number": number, "status": "connected"}])
		api.push_incoming(second["device_uuid"], [
			{"account_id": new, "message_id": msg_id, "chat_jid": "919811122233@s.whatsapp.net", "sender_phone": "919811122233", "message_type": "text", "content": "hi", "timestamp": 1763700000, "history": True},
			{"account_id": new, "message_id": "AFTER-RELINK", "chat_jid": "919811122233@s.whatsapp.net", "sender_phone": "919811122233", "message_type": "text", "content": "new", "timestamp": 1763700100},
		])
		frappe.set_user("Administrator")
		from whatsapp_connector.accounts import merge_previous_accounts

		merge_previous_accounts(new)  # (runs as a background job after the heartbeat)
		self.assertFalse(frappe.db.exists("WhatsApp Account", old))
		ids = frappe.get_all("WhatsApp Message", filters={"account": new}, pluck="whatsapp_message_id")
		self.assertEqual(sorted(ids), sorted([msg_id, "AFTER-RELINK"]))  # one copy of the re-imported message
		self.assertEqual(frappe.db.get_value("WhatsApp Account", new, "user"), self.ALICE)

	def test_login_again_never_duplicates(self):
		from whatsapp_connector.accounts import merge_previous_accounts

		number = "96" + str(uuid.uuid4().int)[:8]
		first = self.login()
		old = self.link_whatsapp(first["device_uuid"], phone=number)
		live_id = f"IN-{old[:8]}"
		frappe.local.whatsapp_device = None
		sent = messaging.queue_message("98111 22233", "sent from ERPNext", account=old)
		self.act_as(first["device_uuid"])
		api.claim_jobs(first["device_uuid"])
		api.report_job_result(first["device_uuid"], sent.name, success=True, whatsapp_message_id="SENT-FROM-ERP")
		api.push_contacts(first["device_uuid"], [{"account_id": old, "jid": "919811122233@s.whatsapp.net", "name": "Customer"}])
		login_api.logout(first["device_uuid"])
		frappe.set_user("Administrator")
		before = frappe.db.count("WhatsApp Message", {"account": old})

		# Log in again (another computer), link the same number: history arrives in
		# several batches, before and after the merge, including the ERPNext-sent one.
		second = self.login()
		new = new_id()
		dev = second["device_uuid"]
		history = lambda mid, from_me=False: {"account_id": new, "message_id": mid, "chat_jid": "919811122233@s.whatsapp.net", "sender_phone": "919811122233", "message_type": "text", "content": "x", "timestamp": 1763700000, "history": True, "from_me": from_me}
		self.act_as(dev)
		api.heartbeat(dev, accounts=[{"account_id": new, "label": "Sales", "phone_number": number, "status": "connected"}])
		api.push_incoming(dev, [history(live_id)])
		frappe.set_user("Administrator")
		merge_previous_accounts(new)
		self.act_as(dev)
		again = api.push_incoming(dev, [history(live_id), history("SENT-FROM-ERP", from_me=True)])
		api.push_incoming(dev, [{**history(live_id), "history": False}])  # per-message path too
		api.push_contacts(dev, [{"account_id": new, "jid": "919811122233@s.whatsapp.net", "name": "Customer"}])
		frappe.set_user("Administrator")

		self.assertEqual(len(again["accepted"]), 0)
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": new}), before)
		for mid in (live_id, "SENT-FROM-ERP"):
			self.assertEqual(frappe.db.count("WhatsApp Message", {"account": new, "whatsapp_message_id": mid}), 1)
		self.assertEqual(frappe.db.count("WhatsApp Contact", {"account": new, "jid": "919811122233@s.whatsapp.net"}), 1)

	def test_new_number_on_same_account_keeps_numbers_apart(self):
		from frappe.utils import add_to_date as add

		result = self.login()
		dev = result["device_uuid"]
		first_number = "95" + str(uuid.uuid4().int)[:8]
		account = self.link_whatsapp(dev, phone=first_number)
		# (stored "before" the switch)
		frappe.db.sql("update `tabWhatsApp Message` set creation = %s where account = %s", (add(now_datetime(), minutes=-5), account))
		# Older apps: the phone logged out, another phone scanned the QR - same account ID.
		second_number = "94" + str(uuid.uuid4().int)[:8]
		self.act_as(dev)
		api.heartbeat(dev, accounts=[{"account_id": account, "label": "Sales", "phone_number": second_number, "status": "connected"}])
		api.push_incoming(dev, [{"account_id": account, "message_id": "NEW-NUMBER-1", "chat_jid": "919811122233@s.whatsapp.net", "sender_phone": "919811122233", "message_type": "text", "content": "x", "timestamp": 1763700000}])
		frappe.set_user("Administrator")

		archive = frappe.db.get_value("WhatsApp Account", {"phone_number": "91" + first_number, "user": self.ALICE})
		self.assertTrue(archive and archive != account)
		self.assertEqual(frappe.db.get_value("WhatsApp Account", archive, "status"), "logged_out")
		self.assertEqual(frappe.get_all("WhatsApp Message", filters={"account": account}, pluck="whatsapp_message_id"), ["NEW-NUMBER-1"])
		self.assertEqual(frappe.db.count("WhatsApp Message", {"account": archive}), 1)

	def test_users_see_only_their_own_connection_row(self):
		self.login()
		self.login(user=self.BOB)
		frappe.set_user(self.ALICE)
		self.assertEqual(frappe.get_list("WhatsApp Connection User", filters={"user": ("in", [self.ALICE, self.BOB])}, pluck="name"), [self.ALICE])
		self.assertFalse(frappe.has_permission("WhatsApp Connection User", "read", self.BOB))
		frappe.set_user(self.MANAGER)
		self.assertEqual(len(frappe.get_list("WhatsApp Connection User", filters={"user": ("in", [self.ALICE, self.BOB])})), 2)

	def test_token_device_isolation(self):
		a = self.login()["device_uuid"]
		b = self.login(user=self.BOB)["device_uuid"]
		account = new_id()
		self.act_as(a)
		self.assertEqual(api.heartbeat(a, accounts=[{"account_id": account, "label": "Sales", "phone_number": "9876543210", "status": "connected"}])["status"], "Approved")
		# Device A's token cannot act as device B, even though both use the service user.
		for call in (lambda: api.heartbeat(b), lambda: api.claim_jobs(b), lambda: login_api.logout(b), lambda: api.register_device(new_id())):
			with self.assertRaises(frappe.PermissionError):
				call()
		frappe.set_user("Administrator")
		frappe.local.whatsapp_device = None
		doc = messaging.queue_message("9876501234", "token flow", account=account)
		self.act_as(b)
		self.assertNotIn(doc.name, [j["job_id"] for j in api.claim_jobs(b)["jobs"]])
		self.act_as(a)
		self.assertEqual([j["job_id"] for j in api.claim_jobs(a)["jobs"]], [doc.name])

	def test_api_key_user_cannot_touch_token_devices(self):
		device_id = self.login()["device_uuid"]
		frappe.set_user(DEVICE_USER)
		with self.assertRaises(frappe.PermissionError):
			api.heartbeat(device_id)
