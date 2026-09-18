"""Direct KPI aggregation, bounded cache and authorization contracts."""

from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_portal import DEPARTMENT, OTHER, FakeRepository, login, provider

from app.core.config import Settings
from app.integrations.iiko.errors import IikoError
from app.portal import create_portal
from app.web.indicators import (
    BASE,
    DEFAULTS,
    IndicatorQuery,
    IndicatorService,
    calculate,
    fetch_values,
    parse_totals,
    request_body,
)
from app.web.settings import WebSettings

DAY = date(2026, 9, 13)
SCOPE = SimpleNamespace(ids=[UUID(int=1)])


@pytest.fixture
def services():
    active = []

    def create(**kwargs):
        service = IndicatorService(None, **kwargs)
        active.append(service)
        return service

    yield create
    for service in active:
        service.close()


def result(*_):
    return {
        "values": [
            calculate({"revenue": Decimal(900), "cost": Decimal(300)}),
            calculate({"revenue": Decimal(400), "cost": Decimal(200)}),
        ],
        "observed_at": datetime.now(UTC),
    }


def finish(service, scope, query, metric):
    service._job(scope, query, metric).result(timeout=5)
    return service.get(scope, query, metric)


def test_ratios_use_amounts_and_missing_is_not_zero():
    totals = calculate(
        dict(
            revenue=Decimal(900),
            cost=Decimal(300),
            checks=Decimal(3),
            guests=Decimal(6),
            discount=Decimal(100),
            gross_revenue=Decimal(1000),
        )
    )
    assert totals["markup"] == 200
    assert totals["discount_percent"] == 10
    assert totals["average_check"] == 300
    assert totals["guests_per_check"] == 2
    assert calculate({"revenue": Decimal(100), "cost": Decimal(0)})["markup"] is None
    assert all(v is None for v in calculate({}).values())


def test_period_validation_and_default_comparison():
    q = IndicatorQuery(start="2025-01-01", end="2025-12-31")
    assert (q.previous_start, q.previous_end) == (date(2024, 1, 2), date(2024, 12, 31))
    assert IndicatorQuery(day=DAY).start == DAY
    for values in [
        dict(start=DAY),
        dict(start="2025-02-01", end="2025-01-01"),
        dict(day=DAY, start=DAY),
        dict(start="2099-01-01", end="2099-01-02"),
        dict(day=DAY, previous_start=DAY),
        dict(day=DAY, previous_start=DAY, previous_end=DAY),
        dict(start="2001-01-01", end=DAY),
    ]:
        with pytest.raises(ValidationError):
            IndicatorQuery(**values)


def test_defaults_allowlist_and_no_daily_grouping():
    plain = IndicatorQuery(day=DAY)
    assert plain.effective_filters() == DEFAULTS
    for filters in [{"Department.Id": ["foreign"]}, {"product": ["x"] * 101}]:
        with pytest.raises(ValidationError):
            IndicatorQuery(day=DAY, filters=filters)
    q = IndicatorQuery(
        start="2025-01-01", end="2025-12-31", filters={"waiter": ["A"], "dish_deleted": []}
    )
    body = request_body(q, ["allowed"], list(BASE.values()), q.start, q.end)
    assert body["filters"]["Department.Id"]["values"] == ["allowed"]
    assert "DeletedWithWriteoff" not in body["filters"]
    assert body["filters"]["OpenDate.Typed"]["from"] == "2025-01-01T00:00:00"
    assert body["filters"]["OpenDate.Typed"]["to"] == "2026-01-01T00:00:00"
    assert body["groupByRowFields"] == []


def test_native_average_missing_and_empty():
    assert parse_totals(
        [{"OrderTime.AveragePrechequeTime": "18.1166666667"}], ["precheck_minutes"]
    )["precheck_minutes"] == Decimal("18.1166666667")
    assert parse_totals([{}], ["revenue"])["revenue"] is None
    assert parse_totals([], ["revenue", "precheck_minutes"]) == {
        "revenue": 0,
        "precheck_minutes": None,
    }
    for rows in [[{}, {}], [{"DishDiscountSumInt": "NaN"}]]:
        with pytest.raises(ValueError):
            parse_totals(rows, ["revenue"])


def test_periods_are_aggregated_by_iiko_without_database_sales(monkeypatch):
    from app.web import indicators

    captured = []

    def collect(_, bodies):
        captured.extend(bodies)
        return [
            [{"OrderTime.AveragePrechequeTime": "20.5"}],
            [{"OrderTime.AveragePrechequeTime": "14"}],
        ]

    monkeypatch.setattr(indicators, "collect_reports", collect)
    value = fetch_values(
        None,
        IndicatorQuery(start="2025-01-01", end="2025-12-31"),
        ["allowed"],
        ["precheck_minutes"],
    )
    assert value["values"][0]["precheck_minutes"] == Decimal("20.5")
    assert len(captured) == 2
    assert all(b["groupByRowFields"] == [] for b in captured)
    assert captured[0]["aggregateFields"] == ["OrderTime.AveragePrechequeTime"]


def test_cache_separates_scope_filters_periods_and_reuses_related_metrics(services):
    calls = []
    now = [10]

    def fetch(*args):
        calls.append(args)
        return result()

    service = services(fetch=fetch, clock=lambda: now[0])
    q = IndicatorQuery(day=DAY)
    assert finish(service, SCOPE, q, "markup")["current"]["value"] == 200
    assert finish(service, SCOPE, q, "gross_profit")["current"]["value"] == 600
    finish(service, SCOPE, IndicatorQuery(day=DAY, filters=DEFAULTS), "markup")
    assert len(calls) == 1
    finish(service, SimpleNamespace(ids=[UUID(int=2)]), q, "markup")
    finish(service, SCOPE, IndicatorQuery(day=DAY, filters={"product": ["x"]}), "markup")
    finish(service, SCOPE, IndicatorQuery(day=date(2026, 9, 12)), "markup")
    assert len(calls) == 4
    now[0] += 301
    finish(service, SCOPE, q, "markup")
    assert len(calls) == 5
    with pytest.raises(HTTPException):
        service.get(SimpleNamespace(ids=[]), q, "markup")
    with pytest.raises(HTTPException):
        service.get(SCOPE, q, "arbitrary")


def test_concurrent_requests_share_one_inflight_job(services):
    entered, release = Event(), Event()
    calls = []

    def fetch(*_):
        entered.set()
        release.wait(5)
        calls.append(1)
        return result()

    service = services(fetch=fetch)
    q = IndicatorQuery(day=DAY)
    try:
        assert service.get(SCOPE, q, "revenue")["status"] == "loading"
        assert entered.wait(2)
        for _ in range(10):
            assert service.get(SCOPE, q, "revenue")["status"] == "loading"
    finally:
        release.set()
    assert finish(service, SCOPE, q, "revenue")["current"]["value"] == 900
    assert calls == [1]


def test_error_cooldown_starts_after_slow_failure(services):
    now = [0]
    calls = []

    def fail(*_):
        calls.append(1)
        now[0] += 90
        raise ValueError("private")

    service = services(fetch=fail, clock=lambda: now[0])
    q = IndicatorQuery(day=DAY)
    with pytest.raises(ValueError):
        service._job(SCOPE, q, "revenue").result(2)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            service.get(SCOPE, q, "revenue")
        assert "private" not in error.value.detail
    assert len(calls) == 1
    now[0] += 31
    with pytest.raises(ValueError):
        service._job(SCOPE, q, "revenue").result(2)
    assert len(calls) == 2


def test_unknown_session_does_not_trigger_new_logins(services):
    calls = []

    def fail(*_):
        calls.append(1)
        raise IikoError("iiko_session_unknown", "private")

    service = services(fetch=fail)
    for _ in range(2):
        with pytest.raises(HTTPException):
            service.get(SCOPE, IndicatorQuery(day=DAY))
    assert calls == [1]


def test_unconfirmed_logout_quarantines_the_direct_service(monkeypatch):
    from contextlib import nullcontext

    from app.web import indicators

    events = []

    class Client:
        def __init__(self, _):
            events.append("client")

        async def aclose(self):
            events.append("closed")

    class Auth:
        def __init__(self, *_):
            pass

        async def download_daily_sales(self, path, **_):
            path.write_text('{"data": []}')

        async def logout(self):
            raise IikoError("iiko_timeout", "timeout", outcome_unknown=True)

    def no_collector(*_, **__):
        raise httpx.ConnectError("local collector absent")

    monkeypatch.setattr(indicators.psycopg, "connect", lambda *a, **k: nullcontext(None))
    monkeypatch.setattr(indicators, "reference_lock", lambda _: nullcontext())
    monkeypatch.setattr(indicators.httpx, "post", no_collector)
    monkeypatch.setattr(indicators, "IikoClient", Client)
    monkeypatch.setattr(indicators, "IikoAuthService", Auth)
    service = IndicatorService(Settings())
    scope = SimpleNamespace(ids=[UUID(int=1)])
    for _ in range(2):
        with pytest.raises(HTTPException) as failure:
            service.get(scope, IndicatorQuery(day=DAY, direct=True))
        assert failure.value.status_code == 503
    assert service.session_unknown
    assert events == ["client", "closed"]


def test_routes_enforce_auth_scope_origin_and_never_query_stored_kpis():
    class Repo(FakeRepository):
        def indicators(self, *_):
            raise AssertionError("No stored KPI reads")

        def indicator_filters(self, scope):
            assert list(scope.ids) == [DEPARTMENT]
            return {"options": {"order_type": ["Обычный"]}, "sync": {}}

    seen = []

    def fetch(_, query, ids, inputs):
        seen.append(ids)
        return result()

    service = IndicatorService(None, fetch=fetch)
    app = create_portal(
        settings=Settings(_env_file=None),
        web_settings=WebSettings(_env_file=None),
        repository=Repo(),
        auth_transport=httpx.MockTransport(provider),
        indicator_service=service,
    )
    body = {"day": str(DAY)}
    headers = {"Origin": "http://127.0.0.1:8013"}
    with TestClient(app) as client:
        assert (
            client.post("/api/indicators/metric/revenue", json=body, headers=headers).status_code
            == 401
        )
        assert client.get("/api/indicators/filters").status_code == 401
        login(client)
        for path in ["/api/indicators/filters", "/api/indicators/options/order_type"]:
            r = (
                client.get(path)
                if path.endswith("filters")
                else client.post(path, json=body, headers=headers)
            )
            assert r.status_code == 200
        assert seen == []
        assert client.get(f"/api/indicators/filters?department_id={OTHER}").status_code == 403
        assert (
            client.post(
                "/api/indicators/metric/revenue",
                json=body,
                headers={"Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/indicators/metric/revenue?department_id={OTHER}", json=body, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post("/api/indicators/metric/arbitrary", json=body, headers=headers).status_code
            == 422
        )
        assert (
            client.post(
                "/api/indicators/metric/revenue",
                json={**body, "filters": {"sql": ["x"]}},
                headers=headers,
            ).status_code
            == 422
        )
        r = client.post("/api/indicators/metric/revenue", json=body, headers=headers)
        assert r.status_code in (200, 202)
        service._job(SimpleNamespace(ids=[DEPARTMENT]), IndicatorQuery(**body), "revenue").result(2)
        r = client.post("/api/indicators/metric/revenue", json=body, headers=headers)
        assert r.status_code == 200 and r.json()["source"] == "iiko_api"
        assert r.json()["current"]["value"] == "900"
        assert seen == [[str(DEPARTMENT)]]
