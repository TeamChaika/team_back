"""Pausing a venue also fences direct OLAP requests and pre-existing caches."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from test_indicators import DAY, result
from test_live_sales import bundle
from test_warehouse_sales import A
from test_warehouse_sales import scope as warehouse_scope

from app.web import indicators, sync_policy
from app.web.indicators import IndicatorQuery, IndicatorService
from app.web.live_sales import LiveSales
from app.web.sync_policy import SyncPolicy, guard_requests
from app.web.warehouse_sales import WarehouseSales

ACTIVE, PAUSED = UUID(int=1), UUID(int=2)


def test_raw_guard_rejects_disabled_and_unscoped_olap(monkeypatch):
    monkeypatch.setattr(
        sync_policy, "read_policy", lambda _: SyncPolicy(2, frozenset([str(ACTIVE)]))
    )

    def body(ids):
        return {"filters": {"Department.Id": {"filterType": "IncludeValues", "values": ids}}}

    guard_requests(None, [body([str(ACTIVE)])])
    for request in [body([str(PAUSED)]), body([str(ACTIVE), str(PAUSED)]), body([]), {}]:
        with pytest.raises(HTTPException) as error:
            guard_requests(None, [request])
        assert error.value.status_code == 423


def test_guard_runs_before_iiko_session_even_for_queued_job(monkeypatch):
    calls = []
    monkeypatch.setattr(indicators.psycopg, "connect", lambda *a, **k: nullcontext(None))
    # SaaS connection wrapper remains exercised with the default non-tenant runtime.
    monkeypatch.setattr(indicators, "reference_lock", lambda _: nullcontext())
    monkeypatch.setattr(sync_policy, "read_policy", lambda _: SyncPolicy(3, frozenset()))
    monkeypatch.setattr(indicators, "IikoClient", lambda *_: calls.append("iiko"))
    with pytest.raises(HTTPException) as error:
        indicators.collect_reports(
            SimpleNamespace(database_url=SimpleNamespace(get_secret_value=lambda: "")), [{}]
        )
    assert error.value.status_code == 423
    assert calls == []


def test_mixed_live_view_falls_back_to_history_without_overlay():
    calls = []
    cache = LiveSales(
        SimpleNamespace(live_sales_cache_seconds=300),
        fetch=lambda _: calls.append(1) or bundle(),
        policy_reader=lambda: SyncPolicy(2, frozenset([str(ACTIVE)])),
    )
    capture, metadata = cache.get(SimpleNamespace(ids=[ACTIVE, PAUSED]))
    assert capture is None and metadata["status"] == "sync_paused"
    assert calls == []
    cache.get(SimpleNamespace(ids=[ACTIVE]))
    assert calls == [1]


def test_live_cache_invalidates_after_policy_change_and_empty_never_fetches():
    state, calls = [SyncPolicy()], []
    cache = LiveSales(
        SimpleNamespace(live_sales_cache_seconds=300),
        fetch=lambda _: calls.append(1) or bundle(),
        policy_reader=lambda: state[0],
    )
    cache.get()
    state[0] = SyncPolicy(2, frozenset([str(ACTIVE)]))
    cache.get(SimpleNamespace(ids=[ACTIVE]))
    assert len(calls) == 2
    state[0] = SyncPolicy(3, frozenset())
    with pytest.raises(HTTPException) as error:
        cache.get()
    assert error.value.status_code == 423 and len(calls) == 2


def test_live_failed_refresh_never_returns_old_cache_after_pause():
    state, clock = [SyncPolicy()], [0]
    cache = LiveSales(
        SimpleNamespace(live_sales_cache_seconds=300),
        fetch=lambda _: bundle(),
        clock=lambda: clock[0],
        policy_reader=lambda: state[0],
    )
    cache.get()

    def paused(_):
        state[0] = SyncPolicy(2, frozenset())
        raise HTTPException(423, "Paused")

    cache.fetch = paused
    clock[0] = 301
    capture, metadata = cache.get(SimpleNamespace(ids=[ACTIVE]))
    assert capture is None and metadata["status"] == "sync_paused"


def test_indicators_do_not_reuse_cache_after_disabling_venue():
    state, calls = [SyncPolicy()], []
    service = IndicatorService(
        None, fetch=lambda *args: calls.append(1) or result(), policy_reader=lambda: state[0]
    )
    try:
        user_scope = SimpleNamespace(ids=[ACTIVE])
        service.get(user_scope, IndicatorQuery(day=DAY))
        state[0] = SyncPolicy(2, frozenset([str(PAUSED)]))
        with pytest.raises(HTTPException) as error:
            service.get(user_scope, IndicatorQuery(day=DAY))
        assert error.value.status_code == 423 and calls == [1]
    finally:
        service.close()


def test_warehouse_cache_blocked_after_pause_and_raw_423_preserved():
    state, calls = [SyncPolicy()], []
    service = WarehouseSales(
        None, fetch=lambda *args: calls.append(1) or {"rows": {}}, policy_reader=lambda: state[0]
    )
    service.get(warehouse_scope(A), DAY, DAY, ["daily"])
    state[0] = SyncPolicy(2, frozenset())
    with pytest.raises(HTTPException) as error:
        service.get(warehouse_scope(A), DAY, DAY, ["daily"])
    assert error.value.status_code == 423 and calls == [1]
    state[0] = SyncPolicy(3)

    def fail(*args):
        raise HTTPException(423, "Paused during queue")

    service.fetch = fail
    with pytest.raises(HTTPException) as error:
        service.get(warehouse_scope(A), DAY, DAY, ["daily"])
    assert error.value.status_code == 423
