"""Commercial invoices share the native document pool, not approval permissions."""

import json
import logging

from app.commercial_invoices.counterparty_transport import CounterpartyTransport
from app.commercial_invoices.service import CommercialInvoiceService
from app.commercial_invoices.transport import CommercialTransport


def build_service(documents, settings):
    if not settings.commercial_enabled or not hasattr(documents, "database"):
        return None
    try:
        seller = json.loads(settings.commercial_seller_json.get_secret_value())
        required = {"name", "inn", "bank_name", "bic", "account", "correspondent_account"}
        if not isinstance(seller, dict) or not required <= seller.keys():
            raise ValueError
        if any(not isinstance(value, str) or len(value) > 2000 for value in seller.values()):
            raise ValueError
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("Commercial invoice seller is not configured")
        return None
    return CommercialInvoiceService(
        documents.database,
        CommercialTransport(settings),
        seller=seller,
        submit_enabled=settings.commercial_submit_enabled,
        counterparty_enabled=settings.commercial_counterparty_create_enabled,
        counterparty_provider=CounterpartyTransport(settings),
    )
