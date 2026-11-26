"""Automatic sending: give the existing DocType rows a Send When."""


def execute():
	from whatsapp_connector.api.document_send import ensure_default_doctypes

	ensure_default_doctypes()
