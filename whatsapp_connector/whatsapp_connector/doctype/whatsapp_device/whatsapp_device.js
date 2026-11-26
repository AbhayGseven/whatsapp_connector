// Copyright (c) 2026, Gseven Computer Business and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Device", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status !== "Approved") {
			frm.add_custom_button(__("Approve"), () => {
				frm.set_value("status", "Approved");
				frm.save();
			}).addClass("btn-primary");
		}
		if (frm.doc.status !== "Disabled") {
			frm.add_custom_button(__("Disable"), () =>
				frappe.confirm(__("Stop this computer from sending and receiving WhatsApp messages?"), () => {
					frm.set_value("status", "Disabled");
					frm.save();
				})
			);
		}

		if (frm.doc.auth_method === "Device Token" && frm.doc.token_issued_on) {
			frm.add_custom_button(__("Reset Token"), () =>
				frappe.confirm(
					__("Log this computer out? WhatsApp is unlinked there and its chats are removed from ERPNext. The user can log in again with their email and password."),
					() =>
						frappe
							.xcall("whatsapp_connector.api.login.reset_device_token", { device: frm.doc.name })
							.then(() => frm.reload_doc())
				)
			);
		}

		frm.page.set_indicator(
			__(frm.doc.connection_status),
			frm.doc.connection_status === "Online" ? "green" : "gray"
		);
	},
});
