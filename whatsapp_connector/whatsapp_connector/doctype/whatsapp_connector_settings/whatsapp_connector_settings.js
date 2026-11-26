// Copyright (c) 2026, Gseven Computer Business and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Connector Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Connected Users"), () => frappe.set_route("List", "WhatsApp Connection User"));
	},
});
