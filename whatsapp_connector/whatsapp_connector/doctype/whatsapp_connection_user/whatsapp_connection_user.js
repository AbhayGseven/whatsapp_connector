// Copyright (c) 2026, Gseven Computer Business and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Connection User", {
	refresh(frm) {
		const manager = frappe.user.has_role(["WhatsApp Manager", "System Manager"]);
		if (frm.is_new() || !manager || !frm.doc.access_token) return;
		frm.add_custom_button(__("Remove Access Token"), () =>
			frappe.confirm(
				__(
					"Log {0} out of the WhatsApp ERP Connector? WhatsApp is unlinked on their computer and its chats are removed from ERPNext. They can log in again with their email and password.",
					[frappe.utils.escape_html(frm.doc.full_name || frm.doc.user)]
				),
				() =>
					frappe
						.xcall("whatsapp_connector.api.login.remove_access_token", { user: frm.doc.user })
						.then(() => {
							frappe.show_alert({ message: __("Logged out"), indicator: "orange" });
							frm.reload_doc();
						})
			)
		);
	},
});
