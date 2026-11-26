// Copyright (c) 2026, Gseven Computer Business and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Message", {
	setup(frm) {
		// Send from: only numbers still linked to a computer.
		frm.set_query("account", () => ({ filters: { status: ["!=", "logged_out"], phone_number: ["is", "set"] } }));
		frm.set_query("send_to_contact", () => ({ filters: frm.doc.account ? { account: frm.doc.account } : {} }));
	},

	onload(frm) {
		if (!frm.is_new() || frm.doc.account) return;
		frappe.xcall("whatsapp_connector.api.chat.get_accounts").then((accounts) => {
			const linked = (accounts || []).filter((a) => a.linked && a.phone_number);
			const own = linked.find((a) => a.user === frappe.session.user) || linked[0];
			if (own) frm.set_value("account", own.name);
		});
	},

	account(frm) {
		if (frm.is_new() && frm.doc.send_to_contact) frm.set_value("send_to_contact", null);
	},

	send_to_contact(frm) {
		if (!frm.doc.send_to_contact) return;
		frappe.db.get_value("WhatsApp Contact", frm.doc.send_to_contact, ["phone", "jid", "is_group"]).then(({ message: c }) => {
			if (!c) return;
			if (c.phone && !c.is_group) {
				frm.set_value("phone", c.phone);
				frm.set_value("chat_jid", null);
			} else {
				frm.set_value("phone", null);
				frm.set_value("chat_jid", c.jid);
			}
		});
	},

	refresh(frm) {
		if (frm.is_new()) {
			frm.set_intro(__("Choose who to send to, write the message (and attach a file if needed), then Save: the WhatsApp is sent from the chosen number."), "blue");
			return;
		}
		frm.disable_save(); // a saved WhatsApp message is a record, not editable
		if (frm.doc.conversation_key && frm.doc.account) {
			frm.add_custom_button(__("Open in WhatsApp Chat"), () => {
				frappe.route_options = { account: frm.doc.account, conversation: frm.doc.conversation_key };
				frappe.set_route("whatsapp-chat");
			});
		}
		if (frm.doc.direction !== "Outgoing") return;

		if (frm.doc.status === "Queued") {
			frm.add_custom_button(__("Cancel Sending"), () =>
				frappe
					.xcall("whatsapp_connector.api.messaging.cancel", { name: frm.doc.name })
					.then(() => frm.reload_doc())
			);
		}

		if (["Failed", "Cancelled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), () =>
				frappe
					.xcall("whatsapp_connector.api.messaging.retry", { name: frm.doc.name })
					.then(() => frm.reload_doc())
			);
		}

		const colors = { Queued: "orange", Sending: "orange", Sent: "blue", Delivered: "green", Read: "green", Failed: "red", Cancelled: "gray" };
		if (colors[frm.doc.status]) frm.page.set_indicator(__(frm.doc.status), colors[frm.doc.status]);
	},
});
