"""Default DocTypes for the "Send on WhatsApp" button."""


def execute():
	from whatsapp_connector.api.document_send import ensure_default_doctypes

	ensure_default_doctypes()
