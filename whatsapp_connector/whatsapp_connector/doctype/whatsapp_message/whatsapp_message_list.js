frappe.listview_settings["WhatsApp Message"] = {
	add_fields: ["status", "direction"],
	get_indicator(doc) {
		const colors = {
			Received: "blue",
			Queued: "orange",
			Sending: "orange",
			Sent: "blue",
			Delivered: "green",
			Read: "green",
			Failed: "red",
			Cancelled: "gray",
		};
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
