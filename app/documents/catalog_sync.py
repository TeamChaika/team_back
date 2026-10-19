"""Refresh document dictionaries after inventory without starting queue processing."""

import logging
from contextlib import contextmanager
from time import monotonic, sleep

from app.documents.config import DocumentSettings
from app.documents.service import DocumentService
from app.sync_references import SyncError

log = logging.getLogger(__name__)


@contextmanager
def _catalog_leader(database):
    from app.documents.worker import worker_leader

    deadline = monotonic() + 30
    while True:
        with worker_leader(database) as leader:
            if leader is not None:
                yield leader
                return
        # Release the failed attempt's transaction before waiting for another cycle.
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SyncError("document_catalog_refresh_busy")
        sleep(min(1, remaining))


def refresh_after_inventory(settings=None):
    try:
        # Document storage and iiko credentials are independent of the analytics source.
        settings = settings if settings is not None else DocumentSettings()
        if not settings.native_enabled:
            return
        from app.documents.worker import refresh_catalogs

        service = DocumentService(settings)
        try:
            # Share even the old worker's lock; never race its in-flight catalog write.
            with _catalog_leader(service.database):
                refresh_catalogs(service)
                if settings.commercial_enabled:
                    from app.commercial_invoices.catalogs import refresh
                    from app.commercial_invoices.counterparty_transport import CounterpartyTransport
                    from app.commercial_invoices.service import CommercialInvoiceService
                    from app.commercial_invoices.transport import CommercialTransport

                    # Seller requisites gate invoice creation, not read-only dictionaries.
                    refresh(
                        CommercialInvoiceService(
                            service.database,
                            CommercialTransport(settings),
                            counterparty_provider=CounterpartyTransport(settings),
                        )
                    )
        finally:
            service.close()
    except Exception as error:
        if isinstance(error, SyncError) and str(error) == "document_catalog_refresh_busy":
            raise
        log.warning("Document inventory catalog refresh failed (%s)", type(error).__name__)
        raise SyncError("document_catalog_refresh_failed") from None
