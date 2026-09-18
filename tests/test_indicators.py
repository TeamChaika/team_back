from datetime import UTC, date, datetime
from decimal import Decimal
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
    EXTRA,
    IndicatorQuery,
    IndicatorService,
    calculate,
    parse_direct,
    request_body,
    stored_result,
)
from app.web.settings import WebSettings

DAY = date(2026, 9, 13)


def test_ratios_use_amounts_and_missing_is_not_zero():
    result = calculate(
        dict(
            revenue=Decimal(900),
            cost=Decimal(300),
            checks=Decimal(3),
            guests=Decimal(6),
            discount=Decimal(100),
            gross_revenue=Decimal(1000),
        )
    )
    assert result["markup"] == 200
    assert result["discount_percent"] == 10
    assert result["average_check"] == 300
    assert result["guests_per_check"] == 2
    assert calculate({"revenue": Decimal(100), "cost": Decimal(0)})["markup"] is None
    assert all(v is None for v in calculate({}).values())


def test_defaults_and_explicit_all_are_different():
    plain = IndicatorQuery(day=DAY)
    assert plain.effective_filters() == DEFAULTS
    assert not plain.needs_iiko
    assert IndicatorQuery(day=DAY, filters={"dish_deleted": []}).needs_iiko
    with pytest.raises(ValidationError):
        IndicatorQuery(day=DAY, filters={"Department.Id": ["foreign"]})
    with pytest.raises(ValidationError):
        IndicatorQuery(day=DAY, filters={"product": ["x"] * 101})


def test_scoped_olap_request_and_option_self_filter_removal():
    q = IndicatorQuery(day=DAY, filters={"waiter": ["A"], "dish_deleted": []})
    body = request_body(q, ["allowed"], fields=list(BASE.values()))
    assert body["filters"]["Department.Id"]["values"] == ["allowed"]
    assert "DeletedWithWriteoff" not in body["filters"]
    assert body["filters"]["OpenDate.Typed"]["from"] == "2026-09-12T00:00:00"
    assert body["filters"]["OpenDate.Typed"]["to"] == "2026-09-14T00:00:00"
    assert len(body["aggregateFields"]) + len(body["groupByRowFields"]) <= 7
    choices = request_body(
        q, ["allowed"], fields=["UniqOrderId"], group="OrderWaiter.Name", omit="waiter"
    )
    assert "OrderWaiter.Name" not in choices["filters"]
    assert "Department.Id" in choices["filters"]


def test_missing_day_and_empty_published_day():
    observed = datetime(2026, 9, 14, 6, tzinfo=UTC)
    coverage = [
        {"business_date": DAY, "kind": kind, "observed_at": observed}
        for kind in ["daily", "dishes", "returns"]
    ]
    result = stored_result(DAY, coverage, [])
    assert result["current"]["totals"]["revenue"] == 0
    assert result["current"]["totals"]["precheck_minutes"] is None
    assert result["previous"]["totals"]["revenue"] is None
    assert result["previous"]["available"] is False


def test_stored_uses_correct_reports_not_sum_of_duplicate_sales_views():
    observed = datetime(2026, 9, 13, 12, tzinfo=UTC)
    coverage = [
        {"business_date": DAY, "kind": kind, "observed_at": observed}
        for kind in ["daily", "dishes", "returns"]
    ]
    rows = [
        dict(
            business_date=DAY,
            kind="daily",
            revenue=Decimal(90),
            cost=Decimal(30),
            checks=Decimal(1),
            guests=Decimal(2),
        ),
        dict(business_date=DAY, kind="dishes", quantity=Decimal("1.5"), revenue=Decimal(90)),
        dict(
            business_date=DAY,
            kind="returns",
            discount=Decimal(10),
            return_sum=Decimal(0),
            revenue=Decimal(90),
        ),
    ]
    report = stored_result(DAY, coverage, rows)
    assert report["current"]["totals"]["revenue"] == 90
    assert report["current"]["totals"]["gross_revenue"] == 100
    assert report["current"]["totals"]["discount_percent"] == 10
    assert report["current"]["totals"]["quantity"] == Decimal("1.5")
    assert report["current"]["partial"]


def test_direct_preserves_iiko_average_and_nulls():
    a = {"OpenDate.Typed": DAY.isoformat(), **{v: 1 for v in BASE.values()}}
    b = {"OpenDate.Typed": DAY.isoformat(), **{v: None for v in EXTRA.values()}}
    b["OrderTime.AveragePrechequeTime"] = "18.1166666667"
    result = parse_direct(IndicatorQuery(day=DAY), [[a], [b]], datetime.now(UTC))
    assert result["current"]["totals"]["precheck_minutes"] == Decimal("18.1166666667")
    assert result["current"]["totals"]["return_sum"] is None
    assert result["previous"]["totals"]["revenue"] == 0
    with pytest.raises(ValueError):
        parse_direct(IndicatorQuery(day=DAY), [[a, a], [b]], datetime.now(UTC))


def test_cache_is_scoped_to_restaurants_filters_date_and_options():
    calls, now = [], [10]

    def fetch(_, query, ids, option):
        calls.append((query.day, ids, option))
        return {"value": len(calls)}

    service = IndicatorService(None, fetch=fetch, clock=lambda: now[0])
    q = IndicatorQuery(day=DAY)
    scope = SimpleNamespace(ids=[UUID(int=1)])
    assert service.get(scope, q) == service.get(scope, q)
    assert len(calls) == 1
    service.get(SimpleNamespace(ids=[UUID(int=2)]), q)
    service.get(scope, IndicatorQuery(day=DAY, filters={"product": ["x"]}))
    service.get(scope, q, option="product")
    assert len(calls) == 4
    now[0] += 301
    service.get(scope, q)
    assert len(calls) == 5
    with pytest.raises(HTTPException):
        service.get(SimpleNamespace(ids=[]), q)
    with pytest.raises(HTTPException):
        service.get(scope, q, option="arbitrary")


def test_unknown_session_does_not_trigger_new_logins():
    calls = []

    def fetch(*_):
        calls.append(1)
        raise IikoError("iiko_session_unknown", "secret must not reach client")

    service = IndicatorService(None, fetch=fetch)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            service.get(SimpleNamespace(ids=["a"]), IndicatorQuery(day=DAY))
        assert "secret" not in error.value.detail
    assert len(calls) == 1


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


def test_routes_enforce_auth_scope_origin_and_allowlist():
    class Repo(FakeRepository):
        def indicators(self, scope, day, *, live_bundle=None):
            return {"ids": [str(i) for i in scope.ids], "day": day.isoformat()}

    seen = []

    def fetch(_, query, ids, option):
        seen.append(ids)
        return {"ids": ids}

    app = create_portal(
        settings=Settings(_env_file=None),
        web_settings=WebSettings(_env_file=None),
        repository=Repo(),
        auth_transport=httpx.MockTransport(provider),
        indicator_service=IndicatorService(None, fetch=fetch),
    )
    body = {"day": str(DAY)}
    headers = {"Origin": "http://127.0.0.1:8013"}
    with TestClient(app) as client:
        assert client.post("/api/indicators/query", json=body, headers=headers).status_code == 401
        login(client)
        assert client.post("/api/indicators/query", json=body, headers=headers).json()["ids"] == [
            str(DEPARTMENT)
        ]
        assert (
            client.post(
                "/api/indicators/query", json=body, headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/indicators/query?department_id={OTHER}", json=body, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/indicators/query",
                json={**body, "filters": {"arbitrary": ["x"]}},
                headers=headers,
            ).status_code
            == 422
        )
        result = client.post(
            "/api/indicators/query", json={**body, "filters": {"product": ["x"]}}, headers=headers
        )
        assert result.status_code == 200
        assert seen == [[str(DEPARTMENT)]]
        assert (
            client.post(
                "/api/indicators/options/Department.Id", json=body, headers=headers
            ).status_code
            == 422
        )
