# WhatsApp Connector (Frappe app)

This is the ERPNext side of the **WhatsApp ERP Connector** desktop bridge. ERPNext owns all business data (devices, accounts, message history and outgoing send jobs). Each computer running the desktop connector is logged in by one ERPNext user and keeps one WhatsApp session and only carries out what this app tells it to.

## Install

```bash
cd ~/frappe-bench-v16
bench --site <site> install-app whatsapp_connector
```

This creates the DocTypes **WhatsApp Connector Settings**, **WhatsApp Device**, **WhatsApp Account** and **WhatsApp Message**, plus three roles:

| Role | For | Can |
| --- | --- | --- |
| WhatsApp Manager | Admins | Change settings, approve or disable devices, see everything |
| WhatsApp User | Staff | Send messages (`messaging.send`), see messages and accounts |
| WhatsApp Device | The internal service user `whatsapp-connector@service.invalid` (created automatically; Website User, no desk, no password) | Call the device API only |

## Connect a computer: log in with email and password

1. Give the person the **WhatsApp User** role (or WhatsApp Manager). Two-factor authentication isn't supported for this login.
2. In the desktop app, open **ERPNext Login** and enter three fields: the **ERPNext URL**, their **email** and **password**.
3. They link **one** WhatsApp number on that computer by scanning the QR code.

The login uses the same checks as the ERPNext login page: wrong-password tracking, lock-out and disabled users. The password is sent once and never stored. ERPNext then issues an **access token** for that computer, and the computer is approved immediately.

**WhatsApp Connection User** has one row per user, created at login. It shows:
- the user,
- their **WhatsApp mobile number**,
- the computer,
- the **access token** (plain text),
- login and logout times.

A WhatsApp User sees only their own row. Managers see all rows.

The access token is stored in plain text on that row (a hash is kept on WhatsApp Device for checking requests). Anyone who can read the row can use the token, so keep WhatsApp Manager and System Manager limited to trusted people.

Rules:

- **One WhatsApp per computer.** Only one WhatsApp Account is kept per WhatsApp Device.
- **One computer per user.** Logging in on another computer logs the first one out.
- **Logging out ends the session.** On the computer:
  - the token stops working,
  - WhatsApp is unlinked.

  The chats, contacts and attachments **stay in ERPNext** with their owner. The WhatsApp Account is marked *logged_out*.
- **What triggers a logout:**
  - **Log out** in the app,
  - **Remove Access Token** on the WhatsApp Connection User row,
  - **Reset Token** on the WhatsApp Device,
  - deleting the WhatsApp Connection User row,
  - disabling the ERPNext user,
  - logging in on another computer,
  - another user logging in on the same computer.
- **Next login.** If the token was removed or is missing, the next login from the app sets a new one in the row.
- **Disable** on a WhatsApp Device blocks that computer from logging in.
- One computer's token can never act as another computer, claim its jobs or read its media.
- `login` is rate-limited to 10 attempts per 10 minutes per IP address.

Legacy option: a user with the *WhatsApp Device* role and their own API key can still register through `register_device`.

## WhatsApp Chat (desk page)

Open **/app/whatsapp-chat** (sidebar: *WhatsApp Chat*). Users with WhatsApp User, WhatsApp Manager or System Manager can use it.

- **Conversations:** one per WhatsApp account and chat (person or group), newest first, with unread badges, last-message preview, ✓/✓✓/blue ✓✓ ticks, search by name, number or text, and a filter by account.
- **Thread:** WhatsApp-style bubbles with day separators, sender names in groups, reply quotes, and inline images, video, audio, documents, locations and contacts. A link back to the ERPNext document appears when a message has a reference. Older messages load on demand.
- **Send:** text, or an image/document (📎, uploaded as a private file) with an optional caption. Press Enter to send and Shift+Enter for a new line. If the connector PC is offline, messages wait in the queue and go out when it reconnects.
- **React:** hover a message and click ☺ to choose 👍 ❤️ 😂 😮 😢 🙏. Click your own reaction to remove it. Each person keeps one reaction per message, as in WhatsApp, and incoming reactions show as badges.
- **New Chat:** start a conversation by phone number.
- **Live:** the page refreshes through Frappe realtime when messages arrive or statuses change, and marks incoming messages as read in ERPNext (this does not send read receipts to WhatsApp).
- **Failed sends** show the reason and a **Retry** link. The WhatsApp Message form has an **Open in WhatsApp Chat** button.

You can also open a chat from your own code: `frappe.route_options = {phone: "9876543210"}; frappe.set_route("whatsapp-chat")`.

## Who sees which chats

Each WhatsApp number (**WhatsApp Account**) belongs to the ERPNext user who linked it (field **Owner**). The owner is set automatically from the user logged in on that computer, and it stays the owner after logout or unlinking.

- A **WhatsApp User** sees only the chats, messages, attachments and WhatsApp contacts of their own numbers, including numbers that are no longer linked. They can send and react only through their own numbers that are still linked.
- **WhatsApp Managers** and **System Managers** see everything.
- A WhatsApp Account with no Owner is visible to managers only.
- When another user logs in on the same computer, the previous user's chats stay with the previous user.
- This applies to the WhatsApp Chat page, list views and reports, REST `/api/resource`, attachments, and live chat updates (sent only to the owner and managers). The logic is in `whatsapp_connector/access.py`.

## Chat history & WhatsApp contacts

Right after a WhatsApp account is linked by QR code, WhatsApp sends the phone's recent chats, contacts, group names and privacy-ID ↔ phone mappings. The connector imports them into ERPNext:

- **Messages** from the last **Import Chat History (days)** days (WhatsApp Connector Settings, default 90). They are stored as normal WhatsApp Messages, marked as read, and shown at their original WhatsApp time. 0 imports contacts only; more than 90 requests the phone's full archive. Photos and documents in history are only downloaded when **Download Media in History** is on.
- **WhatsApp Contact** (a separate list in this app) holds the saved address-book name, the contact's own WhatsApp name, business name, phone, privacy ID (LID), and group names. Chat titles use these names. If an ERPNext Contact with the same phone number already exists it is linked by a read-only lookup. **This app never creates or changes records in ERPNext's Contact DocType.**
- When a privacy ID's phone number becomes known (from contacts or mappings), that person's earlier `@lid` messages are merged into their phone-number conversation.

WhatsApp only sends history once, when the device is linked. To import again, remove the account in the desktop app and link it again with a new QR code.

**Bulk sync without slowing ERPNext:**
- The connector uploads history in batches of 100 with a 1 s pause between them, and contacts in batches of 100 with pauses. New live messages always go first.
- ERPNext stores each history batch with one multi-row INSERT (one duplicate check per batch, no per-message document save). A batch that fails this fast path is retried message by message, so one bad message is rejected on its own.
- ERPNext sends one chat-page refresh per batch instead of one per message.
- Contact sync never queries ERPNext's Contact table.
- Composite indexes (account + conversation, chat, WhatsApp ID, phone, LID) are created on every `bench migrate`.

## Chats are kept after logout and unlinking

When a WhatsApp is **logged out** (on the phone under Linked devices), **unlinked** in the desktop app, or the user **logs out of ERPNext**:

- **Nothing is deleted.** Messages, attachments and WhatsApp Contacts stay in ERPNext with their owner, and stay searchable in the chat page and the lists.
- **The account is marked unlinked.** The WhatsApp Account gets status *logged_out*. Sending or reacting through it is refused until it's linked again.
- **Waiting messages still upload.** Messages the computer received but hadn't uploaded yet are sent after the next login.

**Linking the same number again (same owner)** continues the same history:
- The earlier account's chats are merged into the new account.
- Messages imported again by the new link are not duplicated.
- The merge runs as a background job, so the bench worker must be running.

`whatsapp_connector/purge.py` (`purge_account_data`) can still delete one account's data on purpose, for example from `bench execute`. Nothing calls it automatically.

## Sending messages from ERPNext

### New WhatsApp Message (form)

**WhatsApp Message → + Add**:
1. **WhatsApp Account**: the number to send from. Your own linked number is preselected.
2. **WhatsApp Contact** (optional): a saved contact or group of that number.
3. **Contact Number**: if no contact is chosen, any number with country code.
4. **Message / Caption** and/or **Media**: image, PDF, document, video or audio.
5. **Save**. The message becomes Outgoing / Queued and the computer of that number sends it.

The same rules apply as in WhatsApp Chat:
- **Sender:** only your own linked numbers (managers: any linked number).
- **Content:** at least text or a file.
- **Unlinked numbers** are refused.

**Who can create:** users with the WhatsApp User, WhatsApp Manager or System Manager role.

**After saving** the message cannot be edited, because it is a record. Follow its status on the form (Queued → Sent → Delivered → Read). Use **Cancel Sending** while it is still queued, or **Retry** if it failed.


### Sent automatically when a document is created or submitted

**WhatsApp Connector Settings → Send WhatsApp automatically → DocTypes** decides which documents send a WhatsApp, and when. It starts with:
- **Submit:** Sales Invoice, Sales Order, Quotation, Delivery Note, Purchase Order (with the PDF).
- **New Document:** Customer and Supplier (no PDF).

| Column | Meaning |
|---|---|
| DocType | Which documents |
| Send When | **New Document**, **Submit** or **Cancel**. Empty turns the row off |
| Mobile Field | Field holding the number. If empty, it is found automatically: contact mobile, mobile no, the contact person, or the customer / supplier / party |
| Attach PDF | Attach the document's PDF (not on Cancel) |
| Print Format | Print format for the PDF (empty = the default one) |
| Message Template | Jinja with the document as `doc`, e.g. `Dear {{ doc.customer_name }}, your invoice {{ doc.name }} of {{ doc.get_formatted('grand_total') }} is ready.` |

How it behaves:
- **Sender:** the WhatsApp number of the user who created or submitted the document. If they have none, the **Default Sending Account** is used.
- **Timing:** it runs as a background job after the save commits (the `short` queue, so a worker must run). Saving never waits for the PDF.
- **Once only:** each document event sends at most one message, even if the event runs again.
- **Result on the document:** a comment records it, either *"WhatsApp: queued to +91…"* or why it was skipped (no mobile number, no linked WhatsApp, …).
- **PDF:** kept as a private file on the document.

### From code

```python
from whatsapp_connector.api.messaging import queue_message

msg = queue_message(
    to="+91 98765 43210",
    message="Your invoice is attached",
    attachment=file_url,                 # optional: image -> SEND_IMAGE, PDF -> SEND_PDF, other -> SEND_DOCUMENT
    reference_doctype="Sales Invoice",   # optional link back to the business document
    reference_name=si.name,
    idempotency_key=f"si-{si.name}",     # optional: calling again returns the same message
)
```

REST (for users with WhatsApp User, WhatsApp Manager or System Manager): `POST /api/method/whatsapp_connector.api.messaging.send` with the same arguments. Instead of `to` you can pass `chat_jid` (`<number>@s.whatsapp.net`, a group `…@g.us`, or a privacy-ID contact `…@lid`). That is needed to reply to contacts WhatsApp shows without a phone number, and to post in groups.

Reactions: `queue_reaction(message_name, "👍")` in Python, or `POST …messaging.react {message, emoji}`. An empty emoji removes the reaction. `messaging.cancel(name)` and `messaging.retry(name)` are also available as buttons on the message form.

If no `account` is given, the account marked **Default Sending Account** is used, otherwise any connected account. Messages stay **Queued** in ERPNext until the owning computer is online and claims them.

### Message status

`Queued → Sending` (claimed by the device, with a lease) `→ Sent` (accepted by WhatsApp) `→ Delivered → Read`, or `Failed` / `Cancelled`. Incoming messages are `Received`. Messages typed on the phone itself are recorded as `Outgoing / Sent`.

### Reacting to messages from your own app

```python
# your_app/hooks.py
doc_events = {
    "WhatsApp Message": {
        "after_insert": "your_app.whatsapp.on_new_message",  # check doc.direction == "Incoming"
        "on_update": "your_app.whatsapp.on_status_change",   # Sent / Delivered / Read / Failed
    }
}
```

## Device API (desktop connector contract)

**Login** (no authentication, rate-limited): `POST /api/method/whatsapp_connector.api.login.login` with `usr, pwd, device_uuid, device_name, hostname, platform, app_version`. It returns:
- `device_token` (the access token, also shown on WhatsApp Connection User),
- `user`, `full_name`, `status` and `config`.

Errors: wrong credentials (401), missing WhatsApp role (403), device disabled (403), rate limit (429).

**Logout** (device token): `POST /api/method/whatsapp_connector.api.login.logout` with `device_uuid`. It revokes the token. Chats are kept.

All other endpoints are `POST` (except `download_job_media`), take a JSON body, require `Authorization: WhatsAppDevice <device_uuid>:<device_token>`, and are idempotent. An invalid, reset or disabled token gets HTTP 401.
Base path: `/api/method/whatsapp_connector.api.device.`

| Method | Body | Returns |
| --- | --- | --- |
| `register_device` | `device_uuid, device_name, hostname, platform, app_version` | `status` (`Pending Approval` / `Approved` / `Disabled`), `config` |
| `heartbeat` | `device_uuid, whatsapp_status, accounts:[{account_id, label, phone_number, jid, status}], app_version, last_error` | `status, pending_jobs, config` |
| `push_incoming` | `device_uuid, messages:[{account_id, message_id, chat_jid, sender_jid, sender_phone, sender_name, from_me, is_group, message_type, content, timestamp, quoted_message_id, media:{mimetype, filename, size}, context}]` (max 100) | `accepted:[{message_id, name}], duplicates:[…], rejected:[{message_id, error}]` |
| `upload_incoming_media` | `device_uuid, account_id, message_id, filename, mimetype, content` (base64) | `file_url` |
| `push_contacts` | `device_uuid, contacts:[{account_id, jid, lid, phone, name, notify, verified_name}]` (max 500) | `upserted, skipped`. Partial updates never erase known names |
| `purge_account` | `device_uuid, account_id, remove_account` | The WhatsApp was logged out or unlinked: marks the account *logged_out*. Its chats are kept. (The name is kept for older apps.) |
| `claim_jobs` | `device_uuid, limit` (max 50) | `jobs:[{job_id, command, account_id, to, chat_jid, content, idempotency_key, attempt, lease_expires_at, media:{url, filename, mimetype, size}\|null, reaction:{message_id, from_me, participant}}]`. Commands: SEND_TEXT, SEND_IMAGE, SEND_DOCUMENT, SEND_PDF, SEND_REACTION |
| `report_job_result` | `device_uuid, job_id, success, whatsapp_message_id, error, retryable, sent_at` | `status` |
| `report_receipts` | `device_uuid, receipts:[{account_id, message_id, status: delivered\|read\|played, timestamp}]` | `updated, unknown` |
| `download_job_media` (GET) | `device_uuid, job_id` | file bytes (job must be claimed by this device) |

Errors come back as standard Frappe responses with `exc_type`: `DeviceNotRegistered` (404), `DeviceNotApproved` (403), `ConnectorDisabled` (503), `PermissionError` (403), `ValidationError` (417).

Rules for the client:

- `success: true` only means WhatsApp **accepted** the message. Delivery and read status arrive later through `report_receipts`.
- Set `retryable: true` for temporary failures (account not connected, network). Leave it false for permanent ones (number not on WhatsApp). Retries stop at **Max Send Attempts**.
- A claimed job returns to the queue if no result is reported within **Job Lease**. Keep the IDs of jobs you have already carried out and re-report the stored result instead of sending again.
- Timestamps may be epoch seconds or milliseconds (UTC).

### Real-time wake-up (Frappe's built-in Socket.IO)

There's no custom socket server. The device connects to the site's standard Frappe Socket.IO endpoint (`wss://<site>/socket.io`, namespace `/<site-name>`) with the headers `Authorization: WhatsAppDevice <uuid>:<token>` and `Origin: https://<site>`. The Origin hostname must match the host, because Frappe checks the socket by calling back to the Origin with that header, and this app's auth hook accepts it. All devices share the service user's room, so a device should ignore events whose `device_uuid` isn't its own. This app publishes:

- `whatsapp_connector_jobs` `{device_uuid}`: jobs are waiting, so call `claim_jobs`
- `whatsapp_connector_device_status` `{device_uuid, status}`: the device was approved or disabled

These events are only hints. Devices also poll every **Job Poll Interval** seconds, so a missed event never loses a job.

The site name and Socket.IO address are sent to the device in `config.site_name` / `config.realtime_url`. Behind nginx, leave **Realtime URL** empty, because the socket is served on the site's own origin. On a development bench, set it to the socket.io address (e.g. `http://192.168.1.10:9000`).

## Settings

**WhatsApp Connector Settings** controls: enable/disable, history import (days, media), realtime URL (development only), auto-approve new devices (off by default), default country code (used for 10-digit numbers), heartbeat interval, offline threshold, poll interval, job lease, max attempts and max media size.

A scheduler job runs every minute. It marks devices whose heartbeats have stopped as Offline and puts jobs with expired leases back in the queue. The bench scheduler must be running (`bench start` in development, `bench enable-scheduler` in production).

## Tests

```bash
CI=1 bench --site <site> run-tests --app whatsapp_connector
```

48 integration tests cover contact sync (privacy-ID/phone merge, names, groups, read-only Contact linking), history import, the chat API (conversations, unread, reaction folding), reactions, group/LID targets, email/password login and access tokens (wrong password, missing role, one computer per user, another user on the same computer, logout, manager token removal, disabled user, deleted connection row, visibility of the token row, isolation between devices), registration and approval, device ownership, account sync, incoming de-duplication, media upload, the outgoing lifecycle, retries, lease expiry, routing jobs to the right device, PDF/image commands and permissions. All test data is rolled back.

## License

MIT
