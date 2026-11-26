// WhatsApp Chat: a WhatsApp-style inbox inside ERPNext.
// Messages are sent by the desktop connector; this page only queues them in
// ERPNext (whatsapp_connector.api.messaging) and shows the stored history.

frappe.pages["whatsapp-chat"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("WhatsApp Chat"),
		single_column: true,
	});
	wrapper.whatsapp_chat = new WhatsAppChat(page, wrapper);
};

frappe.pages["whatsapp-chat"].on_page_show = function (wrapper) {
	wrapper.whatsapp_chat?.on_show();
};

const WAC_API = "whatsapp_connector.api.chat";
const WAC_SEND = "whatsapp_connector.api.messaging";
const WAC_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
// 919651292995 -> "+91 96512 92995" (India); other countries: "+<digits>".
function format_number(digits) {
	const d = String(digits || "").replace(/\D/g, "");
	if (d.length === 12 && d.startsWith("91")) return `+91 ${d.slice(2, 7)} ${d.slice(7)}`;
	return d ? `+${d}` : "";
}

const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

class WhatsAppChat {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.accounts = [];
		this.account_filter = "";
		this.current = null; // {account, conversation, send_to}
		this.messages = [];
		this.has_more = false;
		this.attachment = null;
		this.make_layout();
		this.bind_events();
		this.setup_realtime();
		this.load_accounts().then(() => this.refresh_list());
		this.poll = setInterval(() => this.is_visible() && this.refresh_list(), 30000);
	}

	is_visible() {
		return frappe.get_route()[0] === "whatsapp-chat" && !document.hidden;
	}

	on_show() {
		const opts = frappe.route_options;
		frappe.route_options = null;
		if (opts && (opts.conversation || opts.chat_jid || opts.phone)) this.open_from_route(opts);
		else this.refresh_list();
	}

	// ------------------------------------------------------------ layout

	make_layout() {
		this.page.set_primary_action(__("New Chat"), () => this.new_chat(), "add");
		this.$root = $(`
			<div class="wac">
				<aside class="wac-side">
					<div class="wac-side-tools">
						<select class="form-control wac-account-filter"></select>
						<input type="search" class="form-control wac-search" placeholder="${__("Search or start a new chat")}">
					</div>
					<div class="wac-list"></div>
				</aside>
				<section class="wac-main">
					<div class="wac-empty">
						<div class="wac-empty-icon">💬</div>
						<div class="wac-empty-title">${__("WhatsApp Chat")}</div>
						<div class="text-muted">${__("Select a conversation, or start a new chat.")}</div>
					</div>
					<header class="wac-head hidden">
						<div class="wac-avatar wac-head-avatar"></div>
						<div class="wac-head-text">
							<div class="wac-head-title"></div>
							<div class="wac-head-sub text-muted"></div>
						</div>
						<button class="btn btn-default btn-xs wac-open-list" title="${__("Open message list")}">${frappe.utils.icon("list", "sm")}</button>
					</header>
					<div class="wac-thread hidden"></div>
					<div class="wac-attach-preview hidden"></div>
					<footer class="wac-compose hidden">
						<button class="btn wac-attach-btn" title="${__("Attach image or document")}">${frappe.utils.icon("attachment", "md")}</button>
						<textarea class="wac-input" rows="1" placeholder="${__("Type a message")}"></textarea>
						<button class="btn wac-send-btn" title="${__("Send")}">➤</button>
					</footer>
				</section>
			</div>
		`).appendTo(this.page.main);

		this.$list = this.$root.find(".wac-list");
		this.$thread = this.$root.find(".wac-thread");
		this.$input = this.$root.find(".wac-input");
		this.$preview = this.$root.find(".wac-attach-preview");
	}

	bind_events() {
		this.$root.find(".wac-account-filter").on("change", (e) => {
			this.account_filter = e.target.value;
			// A chat of another WhatsApp number must not stay open.
			if (this.current && this.account_filter && this.current.account !== this.account_filter) this.close_chat();
			this.refresh_list();
		});
		this.$root.find(".wac-search").on("input", frappe.utils.debounce(() => this.refresh_list(), 300));

		this.$list.on("click", ".wac-conv", (e) => {
			const $c = $(e.currentTarget);
			this.open_chat($c.attr("data-account"), $c.attr("data-chat"));
		});

		this.$root.find(".wac-send-btn").on("click", () => this.send());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				this.send();
			}
		});
		this.$input.on("input", () => this.autosize());
		this.$root.find(".wac-attach-btn").on("click", () => this.pick_attachment());
		this.$preview.on("click", ".wac-attach-remove", () => this.set_attachment(null));
		this.$root.find(".wac-open-list").on("click", () => {
			if (!this.current) return;
			frappe.set_route("List", "WhatsApp Message", { account: this.current.account, conversation_key: this.current.conversation });
		});

		this.$thread.on("click", ".wac-load-more", () => this.load_messages({ older: true }));
		this.$thread.on("click", ".wac-media-dl", (e) => {
			e.stopPropagation();
			const $btn = $(e.currentTarget);
			this.request_media($btn.attr("data-name"), $btn);
		});
		this.$thread.on("click", ".wac-react-btn", (e) => {
			e.stopPropagation();
			this.show_picker($(e.currentTarget).closest(".wac-msg"));
		});
		this.$thread.on("click", ".wac-reaction", (e) => {
			const $r = $(e.currentTarget);
			const name = $r.closest(".wac-msg").attr("data-name");
			// Clicking your own reaction removes it; any other adds the same emoji.
			this.react(name, $r.data("mine") ? "" : $r.attr("data-emoji"));
		});
		this.$thread.on("click", ".wac-retry", (e) => {
			const name = $(e.currentTarget).closest(".wac-msg").attr("data-name");
			frappe.xcall(`${WAC_SEND}.retry`, { name }).then(() => this.load_messages());
		});
		$(document).on("click", () => $(".wac-picker").remove());
	}

	setup_realtime() {
		const refresh_thread = frappe.utils.debounce(() => this.load_messages({ keep_scroll: true }), 300);
		const refresh_list = frappe.utils.debounce(() => this.refresh_list(), 500);
		frappe.realtime.on("whatsapp_chat_update", (data) => {
			if (!this.is_visible()) return;
			refresh_list();
			if (this.current && data.account === this.current.account && data.conversation === this.current.conversation) {
				refresh_thread();
			}
		});
	}

	// ------------------------------------------------------------ accounts & list

	load_accounts() {
		return frappe.xcall(`${WAC_API}.get_accounts`).then((accounts) => {
			this.accounts = accounts || [];
			const $f = this.$root.find(".wac-account-filter");
			$f.empty().append(`<option value="">${__("All WhatsApp accounts")}</option>`);
			for (const a of this.accounts) {
				$f.append(`<option value="${esc(a.name)}">${esc(this.account_option(a))}</option>`);
			}
			$f.toggle(this.accounts.length > 0);
		});
	}

	// "+91 96512 92995 · Sameer Sagar" (+ state): which WhatsApp number and whose it is.
	account_option(a) {
		const number = a.phone_number ? format_number(a.phone_number) : a.label || __("Number not known yet");
		const state = !a.linked ? __("not linked") : a.device_online ? "" : __("offline");
		return [number, a.user_name].filter(Boolean).join(" · ") + (state ? ` (${state})` : "");
	}

	account_label(name) {
		const a = this.accounts.find((x) => x.name === name);
		return a ? (a.phone_number ? format_number(a.phone_number) : a.label || a.name) : name;
	}

	refresh_list() {
		const search = this.$root.find(".wac-search").val();
		return frappe
			.xcall(`${WAC_API}.get_conversations`, { account: this.account_filter || null, search: search || null })
			.then((rows) => this.render_list(rows || []));
	}

	render_list(rows) {
		if (!rows.length) {
			const text = this.accounts && !this.accounts.length
				? __("No WhatsApp number is assigned to you. Ask a WhatsApp Manager to set you as the Assigned User of your computer (WhatsApp Device).")
				: __("No conversations yet.");
			this.$list.html(`<div class="wac-list-empty text-muted">${text}</div>`);
			return;
		}
		this.$list.html(
			rows
				.map((r) => {
					const active = this.current && this.current.account === r.account && this.current.conversation === r.conversation;
					const out = r.last.direction === "Outgoing";
					return `
					<div class="wac-conv ${active ? "active" : ""}" data-account="${esc(r.account)}" data-chat="${esc(r.conversation)}">
						${this.avatar(r.title, r.is_group)}
						<div class="wac-conv-body">
							<div class="wac-conv-top">
								<span class="wac-conv-title">${esc(r.title)}</span>
								<span class="wac-conv-time ${r.unread ? "unread" : ""}">${this.short_time(r.last_at)}</span>
							</div>
							<div class="wac-conv-bottom">
								<span class="wac-conv-preview">${out ? this.ticks(r.last.status) + " " : ""}${esc(this.preview(r.last))}</span>
								${r.unread ? `<span class="wac-badge">${r.unread}</span>` : ""}
							</div>
							${this.accounts.length > 1 && !this.account_filter ? `<div class="wac-conv-account">${esc(this.account_label(r.account))}</div>` : ""}
						</div>
					</div>`;
				})
				.join("")
		);
	}

	preview(m) {
		if (!m) return "";
		const icons = { image: "📷 ", video: "🎥 ", audio: "🎤 ", document: "📄 ", sticker: "🏷️ ", location: "📍 ", contact: "👤 " };
		if (m.message_type === "reaction") return `${__("Reacted")} ${m.content || ""}`;
		const text = (m.content || m.media_filename || __(frappe.utils.to_title_case(m.message_type || ""))).split("\n")[0];
		return (icons[m.message_type] || "") + text;
	}

	avatar(title, is_group) {
		// Initials from the first letter/digit of up to two words; names made only of
		// emoji or phone numbers get a person icon (avoids splitting emoji in half).
		const letters = String(title || "")
			.split(/\s+/)
			.map((w) => Array.from(w).find((c) => /[\p{L}]/u.test(c)))
			.filter(Boolean)
			.slice(0, 2)
			.join("")
			.toUpperCase();
		const initials = is_group ? "👥" : letters ? esc(letters) : "👤";
		const hue = [...(title || "")].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
		return `<div class="wac-avatar" style="--wac-hue:${hue}">${initials}</div>`;
	}

	// ------------------------------------------------------------ conversation

	open_from_route(opts) {
		const go = (conversation) => {
			const account = opts.account || this.default_account();
			if (!account) {
				frappe.msgprint(__("No WhatsApp account is linked yet."));
				return;
			}
			this.open_chat(account, conversation);
		};
		const jid = opts.conversation || opts.chat_jid;
		if (jid && !jid.endsWith("@s.whatsapp.net")) go(jid);
		else frappe.xcall(`${WAC_API}.resolve_chat`, { phone: jid ? jid.split("@")[0] : opts.phone }).then((r) => go(r.conversation));
	}

	default_account() {
		const a = this.accounts.find((x) => x.is_default) || this.accounts.find((x) => x.status === "connected") || this.accounts[0];
		return a && a.name;
	}

	new_chat() {
		// Only numbers still linked to a computer can start a chat.
		const linked = this.accounts.filter((a) => a.linked);
		if (!linked.length) {
			frappe.msgprint(__("No WhatsApp number is linked. Connect WhatsApp in the desktop app first."));
			return;
		}
		const start = linked.find((a) => a.name === this.account_filter) || linked.find((a) => a.is_default) || linked[0];
		const d = new frappe.ui.Dialog({
			title: __("New WhatsApp Chat"),
			fields: [
				{
					fieldname: "account",
					fieldtype: "Select",
					label: __("Send from"),
					options: linked.map((a) => ({ value: a.name, label: this.account_option(a) })),
					default: start.name,
					reqd: 1,
					hidden: linked.length < 2,
					change: () => d.fields_dict.phone.set_data([]),
				},
				{
					fieldname: "phone",
					fieldtype: "Autocomplete",
					label: __("Phone number or contact"),
					reqd: 1,
					// Pick a WhatsApp contact / group, or type any number.
					ignore_validation: 1,
					max_items: 20,
					description: __("Choose a contact, or type a number with country code, e.g. +91 98765 43210"),
				},
			],
			primary_action_label: __("Open chat"),
			primary_action: (v) => {
				const value = (v.phone || "").trim();
				// Groups and privacy-ID contacts are opened by their chat ID.
				if (value.includes("@")) {
					d.hide();
					this.open_chat(v.account, value);
					return;
				}
				frappe.xcall(`${WAC_API}.resolve_chat`, { phone: value }).then((r) => {
					d.hide();
					this.open_chat(v.account, r.conversation);
				});
			},
		});
		// Contacts of the chosen number, searched as you type (name or digits).
		const phone = d.fields_dict.phone;
		let seq = 0;
		const load = frappe.utils.debounce(() => {
			const ticket = ++seq;
			frappe
				.xcall(`${WAC_API}.search_contacts`, { txt: phone.$input.val(), account: d.get_value("account") })
				.then((rows) => {
					if (ticket !== seq) return; // an older search finished late
					phone.set_data(rows || []);
					if (document.activeElement === phone.$input[0]) phone.awesomplete.evaluate();
				});
		}, 250);
		phone.$input.on("input focus", load);
		d.show();
	}

	close_chat() {
		this.current = null;
		this.messages = [];
		this.has_more = false;
		this.set_attachment(null);
		this.$input.val("");
		this.autosize();
		$(".wac-picker").remove();
		this.$thread.empty();
		this.$root.find(".wac-head, .wac-thread, .wac-compose").addClass("hidden");
		this.$root.find(".wac-empty").removeClass("hidden");
		this.$list.find(".wac-conv").removeClass("active");
	}

	open_chat(account, conversation) {
		this.current = { account, conversation, send_to: null };
		this.messages = [];
		this.set_attachment(null);
		this.$root.find(".wac-empty").addClass("hidden");
		this.$root.find(".wac-head, .wac-thread, .wac-compose").removeClass("hidden");
		this.$list.find(".wac-conv").removeClass("active");
		this.$list.find(`.wac-conv[data-account="${CSS.escape(account)}"][data-chat="${CSS.escape(conversation)}"]`).addClass("active");
		this.$thread.html(`<div class="wac-loading text-muted">${__("Loading…")}</div>`);
		this.load_messages().then(() => this.$input.trigger("focus"));
	}

	load_messages({ older = false, keep_scroll = false } = {}) {
		if (!this.current) return Promise.resolve();
		const { account, conversation } = this.current;
		const before = older && this.messages.length ? this.messages[0].sort_at : null;
		const el = this.$thread[0];
		const at_bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
		const prev_height = el.scrollHeight;

		return frappe.xcall(`${WAC_API}.get_messages`, { account, conversation, before }).then((r) => {
			if (!this.current || this.current.conversation !== conversation || this.current.account !== account) return;
			this.current.send_to = r.send_to;
			this.messages = older ? [...r.messages, ...this.messages] : r.messages;
			if (!older) this.has_more = r.has_more;
			else this.has_more = r.has_more;
			this.render_head(r);
			this.render_thread();
			if (older) el.scrollTop = el.scrollHeight - prev_height;
			else if (!keep_scroll || at_bottom) el.scrollTop = el.scrollHeight;
			if (r.messages.some((m) => m.direction === "Incoming")) {
				frappe.xcall(`${WAC_API}.mark_read`, { account, conversation }).then(() => this.refresh_list());
			}
		});
	}

	render_head(r) {
		const sub = [r.phone ? `+${r.phone}` : r.is_group ? __("Group") : "", this.accounts.length > 1 ? __("via {0}", [this.account_label(this.current.account)]) : ""]
			.filter(Boolean)
			.join(" · ");
		this.$root.find(".wac-head-avatar").replaceWith($(this.avatar(r.title, r.is_group)).addClass("wac-head-avatar"));
		this.$root.find(".wac-head-title").text(r.title);
		this.$root.find(".wac-head-sub").text(sub);
		const account = this.accounts.find((a) => a.name === this.current.account);
		this.$root
			.find(".wac-input")
			.attr("placeholder", account && !account.device_online ? __("Type a message (the connector PC is offline; it will be sent when it reconnects)") : __("Type a message"));
	}

	render_thread() {
		let html = this.has_more ? `<div class="wac-load-more-wrap"><button class="btn btn-xs btn-default wac-load-more">${__("Load older messages")}</button></div>` : "";
		let last_day = null;
		for (const m of this.messages) {
			const when = m.message_timestamp || m.creation;
			const day = when.slice(0, 10);
			if (day !== last_day) {
				html += `<div class="wac-day"><span>${esc(this.day_label(day))}</span></div>`;
				last_day = day;
			}
			html += this.render_message(m);
		}
		if (!this.messages.length) html += `<div class="wac-thread-empty text-muted">${__("No messages yet. Say hello!")}</div>`;
		this.$thread.html(html);
	}

	render_message(m) {
		const out = m.direction === "Outgoing";
		const time = frappe.datetime.str_to_user(m.message_timestamp || m.creation).split(" ").pop().slice(0, 5);
		const failed = m.status === "Failed";
		const sender = !out && this.current.conversation.endsWith("@g.us") && m.sender_name ? `<div class="wac-sender">${esc(m.sender_name)}</div>` : "";
		const quoted = m.quoted
			? `<div class="wac-quoted"><div class="wac-quoted-who">${m.quoted.direction === "Outgoing" ? __("You") : esc(m.quoted.sender_name || __("Contact"))}</div>${esc(this.preview(m.quoted))}</div>`
			: "";
		const reactions = (m.reactions || [])
			.map(
				(r) =>
					`<span class="wac-reaction ${r.mine ? "mine" : ""} ${r.pending ? "pending" : ""}" data-emoji="${esc(r.emoji)}" data-mine="${r.mine ? 1 : ""}" title="${r.mine ? __("Click to remove your reaction") : ""}">${esc(r.emoji)}${r.count > 1 ? ` ${r.count}` : ""}</span>`
			)
			.join("");
		const can_react = Boolean(m.whatsapp_message_id);
		const ref = m.reference_doctype && m.reference_name ? `<a class="wac-ref" href="/app/${frappe.router.slug(m.reference_doctype)}/${encodeURIComponent(m.reference_name)}">${esc(m.reference_doctype)} ${esc(m.reference_name)}</a>` : "";

		return `
		<div class="wac-msg ${out ? "out" : "in"} ${failed ? "failed" : ""}" data-name="${esc(m.name)}">
			<div class="wac-bubble">
				${sender}${quoted}
				${this.render_media(m)}
				${this.render_text(m)}
				${ref}
				<div class="wac-meta">${time}${out ? " " + this.ticks(m.status, m.error) : ""}</div>
				${can_react ? `<button class="wac-react-btn" title="${__("React")}">☺</button>` : ""}
			</div>
			${failed ? `<div class="wac-failed-note">${esc(m.error || __("Not sent"))} · <a class="wac-retry">${__("Retry")}</a></div>` : ""}
			${reactions ? `<div class="wac-reactions">${reactions}</div>` : ""}
		</div>`;
	}

	render_text(m) {
		if (!m.content) return "";
		if (m.message_type === "location") {
			const [coords, ...rest] = m.content.split("\n");
			const url = `https://www.google.com/maps?q=${encodeURIComponent(coords)}`;
			return `<div class="wac-text">📍 <a href="${url}" target="_blank" rel="noopener noreferrer">${esc(rest.join(", ") || coords)}</a></div>`;
		}
		if (m.message_type === "contact") {
			const names = [...m.content.matchAll(/^FN:(.*)$/gm)].map((x) => x[1]);
			const tels = [...m.content.matchAll(/TEL[^:]*:(.*)$/gm)].map((x) => x[1]);
			return `<div class="wac-text">👤 ${esc(names.join(", ") || __("Contact"))}${tels.length ? `<br><span class="text-muted">${esc(tels.join(", "))}</span>` : ""}</div>`;
		}
		return `<div class="wac-text">${esc(m.content).replace(/\n/g, "<br>")}</div>`;
	}

	render_media(m) {
		const url = m.media_file;
		const type = m.message_type;
		const mime = m.media_mimetype || "";
		if (!url) {
			if (!["image", "video", "audio", "document", "sticker"].includes(type)) return "";
			return this.render_media_placeholder(m);
		}
		const safe = esc(url);
		if (type === "image" || type === "sticker" || (type !== "document" && mime.startsWith("image/"))) {
			return `<a href="${safe}" target="_blank" rel="noopener"><img class="wac-img ${type === "sticker" ? "sticker" : ""}" src="${safe}" loading="lazy" alt=""></a>`;
		}
		if (type === "video" || mime.startsWith("video/")) return `<video class="wac-video" src="${safe}" controls preload="metadata"></video>`;
		if (type === "audio" || mime.startsWith("audio/")) return `<audio class="wac-audio" src="${safe}" controls preload="metadata"></audio>`;
		const size = m.media_size ? ` · ${frappe.form.formatters.FileSize ? frappe.form.formatters.FileSize(m.media_size) : Math.round(m.media_size / 1024) + " KB"}` : "";
		const icon = mime === "application/pdf" ? "📕" : "📄";
		return `<a class="wac-doc" href="${safe}" target="_blank" rel="noopener"><span class="wac-doc-icon">${icon}</span><span class="wac-doc-name">${esc(m.media_filename || __("Document"))}</span><span class="text-muted wac-doc-size">${esc(size)}</span></a>`;
	}

	// Media not yet in ERPNext: WhatsApp's preview + Download (fetched on request by
	// the computer where this WhatsApp is linked).
	render_media_placeholder(m) {
		const type = m.message_type;
		const icons = { image: "📷", video: "🎥", audio: "🎤", sticker: "🏷️", document: m.media_mimetype === "application/pdf" ? "📕" : "📄" };
		const name = esc(m.media_filename || __(frappe.utils.to_title_case(type)));
		const size = m.media_size ? ` · ${frappe.form.formatters.FileSize ? frappe.form.formatters.FileSize(m.media_size) : Math.round(m.media_size / 1024) + " KB"}` : "";
		const preview = m.thumbnail ? `<img class="wac-thumb" src="${esc(m.thumbnail)}" alt="">` : `<span class="wac-doc-icon">${icons[type] || "📎"}</span>`;
		const button = (label) => `<button class="btn btn-xs btn-default wac-media-dl" data-name="${esc(m.name)}">⬇ ${label}</button>`;
		let status;
		switch (m.media_note) {
			case "on_request":
				status = button(__("Download"));
				break;
			case "requested":
				status = `<span class="text-muted wac-media-busy">${__("Downloading…")}</span>`;
				break;
			case "failed":
				status = `<span class="wac-media-error" title="${esc(m.media_error || "")}">${__("Download failed")}</span> ${button(__("Try again"))}`;
				break;
			case "too_large":
				status = `<span class="text-muted">${__("Larger than the media size limit")}</span>`;
				break;
			case "pending":
				status = `<span class="text-muted">${__("Uploading…")}</span>`;
				break;
			default:
				status = `<span class="text-muted">${__("Not available")}</span>`;
		}
		return `<div class="wac-media-ph ${m.thumbnail ? "has-thumb" : ""}">${preview}
			<div class="wac-media-ph-body"><div class="wac-doc-name">${name}<span class="text-muted">${esc(size)}</span></div><div>${status}</div></div></div>`;
	}

	request_media(name, $btn) {
		$btn.prop("disabled", true).text(__("Requesting…"));
		frappe
			.xcall("whatsapp_connector.api.chat.request_media", { message: name })
			.then((r) => {
				if (r && r.requested && !r.computer_online) {
					frappe.show_alert({
						message: __("The computer for this WhatsApp is offline. The file downloads when it is back online."),
						indicator: "orange",
					});
				}
				return this.load_messages({ keep_scroll: true });
			})
			.catch(() => $btn.prop("disabled", false).text(__("Download")));
	}

	ticks(status, error) {
		switch (status) {
			case "Queued":
			case "Sending":
				return `<span class="wac-tick pending" title="${__(status)}">🕓</span>`;
			case "Sent":
				return `<span class="wac-tick" title="${__("Sent")}">✓</span>`;
			case "Delivered":
				return `<span class="wac-tick" title="${__("Delivered")}">✓✓</span>`;
			case "Read":
				return `<span class="wac-tick read" title="${__("Read")}">✓✓</span>`;
			case "Failed":
				return `<span class="wac-tick failed" title="${esc(error || __("Failed"))}">⚠</span>`;
			case "Cancelled":
				return `<span class="wac-tick" title="${__("Cancelled")}">⊘</span>`;
			default:
				return "";
		}
	}

	day_label(day) {
		const today = frappe.datetime.get_today();
		if (day === today) return __("Today");
		if (day === frappe.datetime.add_days(today, -1)) return __("Yesterday");
		return frappe.datetime.str_to_user(day);
	}

	short_time(dt) {
		if (!dt) return "";
		const day = dt.slice(0, 10);
		if (day === frappe.datetime.get_today()) return frappe.datetime.str_to_user(dt).split(" ").pop().slice(0, 5);
		return this.day_label(day);
	}

	// ------------------------------------------------------------ reactions

	show_picker($msg) {
		$(".wac-picker").remove();
		const name = $msg.attr("data-name");
		const $p = $(
			`<div class="wac-picker">${WAC_EMOJIS.map((e) => `<button class="wac-pick" data-emoji="${e}">${e}</button>`).join("")}</div>`
		);
		// Attached to <body> so no page container (overflow/transform) can clip or hide it.
		$(document.body).append($p);
		// Above the bubble, or below it when there is no room; aligned to the bubble's side.
		const bubble = $msg.find(".wac-bubble")[0].getBoundingClientRect();
		const thread = this.$thread[0].getBoundingClientRect();
		const width = $p.outerWidth();
		const height = $p.outerHeight();
		const top = bubble.top - height - 6 >= thread.top ? bubble.top - height - 6 : bubble.bottom + 6;
		const left = $msg.hasClass("out") ? Math.max(thread.left + 4, bubble.right - width) : Math.min(bubble.left, thread.right - width - 4);
		$p.css({ top: `${top}px`, left: `${left}px` });
		this.$thread.one("scroll", () => $p.remove());
		$p.on("click", ".wac-pick", (e) => {
			e.stopPropagation();
			$p.remove();
			this.react(name, $(e.currentTarget).attr("data-emoji"));
		});
	}

	react(name, emoji) {
		return frappe
			.xcall(`${WAC_SEND}.react`, { message: name, emoji })
			.then(() => this.load_messages({ keep_scroll: true }));
	}

	// ------------------------------------------------------------ composer

	autosize() {
		const el = this.$input[0];
		el.style.height = "auto";
		el.style.height = Math.min(el.scrollHeight, 140) + "px";
	}

	pick_attachment() {
		new frappe.ui.FileUploader({
			allow_multiple: false,
			make_attachments_public: false,
			on_success: (file) => this.set_attachment(file),
		});
	}

	set_attachment(file) {
		this.attachment = file;
		if (!file) {
			this.$preview.addClass("hidden").empty();
			return;
		}
		const is_image = /\.(png|jpe?g|gif|webp)$/i.test(file.file_name || file.file_url);
		this.$preview.removeClass("hidden").html(`
			<div class="wac-attach-chip">
				${is_image ? `<img src="${esc(file.file_url)}" alt="">` : `<span class="wac-doc-icon">📄</span>`}
				<span class="wac-doc-name">${esc(file.file_name)}</span>
				<button class="btn btn-xs btn-default wac-attach-remove" title="${__("Remove")}">✕</button>
			</div>`);
		this.$input.attr("placeholder", __("Add a caption (optional)")).trigger("focus");
	}

	send() {
		if (!this.current || !this.current.send_to || this.sending) return;
		const message = this.$input.val().trim();
		if (!message && !this.attachment) return;
		this.sending = true;
		const args = {
			account: this.current.account,
			chat_jid: this.current.send_to,
			message: message || null,
			attachment: this.attachment ? this.attachment.file_url : null,
		};
		frappe
			.xcall(`${WAC_SEND}.send`, args)
			.then(() => {
				this.$input.val("");
				this.autosize();
				this.set_attachment(null);
				this.$input.attr("placeholder", __("Type a message"));
				return this.load_messages();
			})
			.then(() => this.refresh_list())
			.finally(() => {
				this.sending = false;
			});
	}
}
