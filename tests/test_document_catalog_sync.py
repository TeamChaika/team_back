"""Catalog refresh uses fake services only: no production storage or external writes."""

from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.documents import catalog_sync, worker
from app.sync_references import SyncError


@pytest.fixture
def catalogs(monkeypatch):
    from app.commercial_invoices import catalogs as commercial_catalogs
    from app.commercial_invoices import counterparty_transport, transport

    settings = SimpleNamespace(native_enabled=True, commercial_enabled=True)
    service = SimpleNamespace(database=object(), close=Mock())
    factory = Mock(return_value=service)
    native = Mock()
    commercial = Mock()
    leader = SimpleNamespace(available=True, active=False, attempts=0)
    clock = SimpleNamespace(now=0, waits=[])

    def wait(seconds):
        assert not leader.active
        clock.waits.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(catalog_sync, "monotonic", lambda: clock.now)
    monkeypatch.setattr(catalog_sync, "sleep", wait)

    @contextmanager
    def lock(database):
        assert database is service.database
        leader.attempts += 1
        leader.active = True
        try:
            yield object() if leader.available else None
        finally:
            leader.active = False

    def require_lock(*args):
        assert leader.active

    native.side_effect = commercial.side_effect = require_lock
    monkeypatch.setattr(catalog_sync, "DocumentSettings", Mock(return_value=settings))
    monkeypatch.setattr(catalog_sync, "DocumentService", factory)
    monkeypatch.setattr(worker, "worker_leader", lock)
    monkeypatch.setattr(worker, "refresh_catalogs", native)
    monkeypatch.setattr(commercial_catalogs, "refresh", commercial)
    # Read-only refresh must never enter either delivery or Telegram processing.
    for name in ("process_jobs", "deliver_one", "poll", "Telegram"):
        monkeypatch.setattr(worker, name, Mock(side_effect=AssertionError("external delivery")))
    for module, name in (
        (transport, "CommercialTransport"),
        (counterparty_transport, "CounterpartyTransport"),
    ):
        monkeypatch.setattr(module, name, Mock(return_value=object()))
    return SimpleNamespace(
        settings=settings,
        service=service,
        factory=factory,
        native=native,
        commercial=commercial,
        leader=leader,
        clock=clock,
    )


def test_refresh_uses_document_settings_both_catalogs_and_closes(catalogs):
    # No seller JSON is configured: dictionary reads must still run.
    catalog_sync.refresh_after_inventory()
    catalogs.factory.assert_called_once_with(catalogs.settings)
    catalogs.native.assert_called_once_with(catalogs.service)
    commercial = catalogs.commercial.call_args.args[0]
    assert commercial.database is catalogs.service.database
    assert commercial.submit_enabled is False
    assert commercial.counterparty_provider is not None
    catalogs.service.close.assert_called_once_with()
    assert not catalogs.leader.active


def test_disabled_documents_do_not_open_storage(catalogs):
    catalogs.settings.native_enabled = False
    catalog_sync.refresh_after_inventory()
    catalogs.factory.assert_not_called()
    catalogs.native.assert_not_called()
    catalogs.commercial.assert_not_called()


def test_disabled_commercial_is_skipped(catalogs):
    catalogs.settings.commercial_enabled = False
    catalog_sync.refresh_after_inventory()
    catalogs.native.assert_called_once()
    catalogs.commercial.assert_not_called()
    catalogs.service.close.assert_called_once()


def test_busy_old_worker_preserves_catalogs_and_closes(catalogs):
    catalogs.leader.available = False
    with pytest.raises(SyncError, match="^document_catalog_refresh_busy$"):
        catalog_sync.refresh_after_inventory()
    catalogs.native.assert_not_called()
    catalogs.commercial.assert_not_called()
    catalogs.service.close.assert_called_once()
    assert catalogs.clock.now == 30
    assert catalogs.clock.waits == [1] * 30
    assert catalogs.leader.attempts == 31


def test_busy_worker_releases_before_waiting_then_refreshes_once(catalogs, monkeypatch):
    catalogs.leader.available = False

    def wait(seconds):
        assert not catalogs.leader.active
        catalogs.clock.now += seconds
        catalogs.clock.waits.append(seconds)
        if len(catalogs.clock.waits) == 2:
            catalogs.leader.available = True

    monkeypatch.setattr(catalog_sync, "sleep", wait)
    catalog_sync.refresh_after_inventory()
    assert catalogs.clock.waits == [1, 1]
    assert catalogs.leader.attempts == 3
    catalogs.native.assert_called_once_with(catalogs.service)
    catalogs.commercial.assert_called_once()
    catalogs.service.close.assert_called_once()


@pytest.mark.parametrize("failing", ["native", "commercial"])
def test_failure_has_fixed_code_and_closes(catalogs, caplog, failing):
    getattr(catalogs, failing).side_effect = ValueError("private token and url")
    with pytest.raises(SyncError, match="^document_catalog_refresh_failed$") as caught:
        catalog_sync.refresh_after_inventory()
    assert caught.value.__suppress_context__
    assert "ValueError" in caplog.text
    assert "private token" not in caplog.text
    catalogs.service.close.assert_called_once()
    assert not catalogs.leader.active


@pytest.mark.parametrize("namespace", ["scheduled", "manual"])
@pytest.mark.parametrize("failed", [False, True])
def test_inventory_scheduler_requires_document_catalog_refresh(
    monkeypatch, tmp_path, namespace, failed
):
    from app import scheduler, sync_inventory
    from app.services import sync_jobs

    calls = []
    analytics_settings = object()
    monkeypatch.setattr(sync_jobs, "ROOT", tmp_path)

    def inventory(settings, *args):
        assert settings is analytics_settings
        calls.append("inventory")

    def refresh():
        calls.append("catalogs")
        if failed:
            raise SyncError("document_catalog_refresh_failed")

    monkeypatch.setattr(sync_inventory, "synchronize_inventory", inventory)
    monkeypatch.setattr(catalog_sync, "refresh_after_inventory", refresh)
    monkeypatch.setattr(scheduler, "prepare_purchase_impact", lambda *_: calls.append("prices"))

    def run():
        scheduler.run_job(
            scheduler.Job("inventory", "Inventory"),
            datetime(2026, 10, 19, tzinfo=UTC),
            analytics_settings,
            Event(),
            namespace=namespace,
        )

    if failed:
        with pytest.raises(SyncError, match="^document_catalog_refresh_failed$"):
            run()
        assert calls == ["inventory", "catalogs"]
    else:
        run()
        assert calls == ["inventory", "catalogs", "prices"]


@pytest.mark.parametrize("stage", ["settings", "service", "close"])
def test_initialization_and_cleanup_errors_have_fixed_code(catalogs, monkeypatch, stage):
    error = RuntimeError("private configuration")
    if stage == "settings":
        monkeypatch.setattr(catalog_sync, "DocumentSettings", Mock(side_effect=error))
    elif stage == "service":
        catalogs.factory.side_effect = error
    else:
        catalogs.service.close.side_effect = error
    with pytest.raises(SyncError, match="^document_catalog_refresh_failed$"):
        catalog_sync.refresh_after_inventory()
    if stage == "close":
        catalogs.service.close.assert_called_once()


def test_provider_sync_error_does_not_escape_with_private_text(catalogs):
    catalogs.native.side_effect = SyncError("private upstream diagnostic")
    with pytest.raises(SyncError, match="^document_catalog_refresh_failed$"):
        catalog_sync.refresh_after_inventory()
    catalogs.service.close.assert_called_once()
