// Copyright (c) 2026, Gseven Computer Business and contributors
// For license information, please see license.txt

frappe.listview_settings["WhatsApp Connection User"] = {
	add_fields: ["status", "whatsapp_status"],
	get_indicator(doc) {
		if (doc.status !== "Connected") return [__("ERPNext Disconnected"), "red", "status,=,Disconnected"];
		if (doc.whatsapp_status === "Connected") return [__("WhatsApp Connected"), "green", "whatsapp_status,=,Connected"];
		if (doc.whatsapp_status === "Connecting") return [__("WhatsApp Connecting"), "orange", "whatsapp_status,=,Connecting"];
		return [__("WhatsApp Disconnected"), "red", "whatsapp_status,=,Disconnected"];
	},
	refresh(listview) {
		// Statuses change from the computers; keep the list current.
		if (!listview.__wa_timer) listview.__wa_timer = setInterval(() => listview.refresh(), 15000);
	},
};
