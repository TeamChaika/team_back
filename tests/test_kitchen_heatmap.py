from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from test_portal import DEPARTMENT, OTHER, FakeRepository, login, provider

from app.core.config import Settings
from app.portal import create_portal
from app.web import indicators, sync_policy
from app.web.kitchen_heatmap import HeatmapTotals, daily_request, local_instant
from app.web.repository import Scope
from app.web.settings import WebSettings
from app.web.sync_policy import SyncPolicy

DAY = date(2026, 9, 10)
STORE = UUID(int=4)
STATION = UUID(int=55)
ENDPOINT = "/api/sales/kitchen/heatmap"


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example.invalid/resto/api",
        iiko_login="synthetic",
        iiko_password="synthetic",
    )


class KitchenRepository(FakeRepository):
    sections = ["sales"]
    restricted = False
    stores = (STORE,)

    def scope(self, user_id, selected=None):
        original = super().scope(user_id, selected)
        original.user.update(
            sections=self.sections, warehouse_scope_mode="selected" if self.restricted else "all"
        )
        return Scope(original.user, original.departments, selected, self.stores, original.rms_ids)


def open_portal(settings, repo, fetch=None):
    return TestClient(
        create_portal(
            settings,
            WebSettings(_env_file=None, anon_key="test"),
            auth_transport=httpx.MockTransport(provider),
            repository=repo,
            kitchen_heatmap_fetch=fetch,
        ),
        base_url="http://127.0.0.1:8013",
    )


def params(*, start=DAY, end=DAY, department=DEPARTMENT):
    return {"department_id": str(department), "date_from": start, "date_to": end}


def row(day, number, **overrides):
    return {
        "Department.Id": str(DEPARTMENT),
        "Store.Id": str(STORE),
        "UniqOrderId.Id": str(UUID(int=100)),
        "ItemSaleEvent.Id": str(UUID(int=1000 + number)),
        "CookingPlace.Id": str(STATION),
        "CookingPlace": "Горячий цех",
        "OpenDate.Typed": str(day),
        "DishServicePrintTime": f"{day}T23:58:30",
        "DishDiscountSumInt": "100.19",
        "DishAmountInt": "1.5",
        "Storned": "FALSE",
        "OrderDeleted": "NOT_DELETED",
        "DeletedWithWriteoff": "NOT_DELETED",
        **overrides,
    }


def test_route_requires_one_authorized_venue_and_sales_permission(settings, monkeypatch):
    monkeypatch.delenv("RESTCONTROL_RUNTIME_MODE", raising=False)
    repo = KitchenRepository()
    calls = []

    def fetch(*_, **__):
        calls.append("fetch")

    with open_portal(settings, repo, fetch) as client:
        assert client.get(ENDPOINT, params=params()).status_code == 401
        assert login(client).status_code == 200
        assert client.get("/api/me").json()["kitchen_heatmap_enabled"] is True
        assert client.get(ENDPOINT, params=params(department=OTHER)).status_code == 403
        assert client.get(ENDPOINT, params=params(department="not-a-uuid")).status_code == 422
        repeated = {**params(), "department_id": [str(DEPARTMENT)] * 2}
        assert client.get(ENDPOINT, params=repeated).status_code == 422
        without_department = {"date_from": DAY, "date_to": DAY}
        assert client.get(ENDPOINT, params=without_department).status_code == 422
        assert client.get(ENDPOINT, params=params(start=f"{DAY}T00:00:00")).status_code == 422
        assert client.get(ENDPOINT, params=params(start=DAY + timedelta(days=1))).status_code == 422
        assert client.get(ENDPOINT, params=params(end=DAY + timedelta(days=7))).status_code == 422
        repo.sections = ["overview"]
        assert client.get("/api/me").json()["kitchen_heatmap_enabled"] is False
        assert client.get(ENDPOINT, params=params()).status_code == 403
        repo.sections = ["sales"]
        repo.restricted = True
        repo.stores = ()
        assert client.get("/api/me").json()["kitchen_heatmap_enabled"] is False
        assert client.get(ENDPOINT, params=params()).status_code == 403
    assert calls == []


def test_tenant_runtime_is_blocked_even_with_source_configured(settings, monkeypatch):
    monkeypatch.setenv("RESTCONTROL_RUNTIME_MODE", "tenant")

    def fetch(*_, **__):
        pytest.fail("Tenant cannot reach the primary OLAP transport")

    with open_portal(settings, KitchenRepository(), fetch) as client:
        login(client)
        assert client.get("/api/me").json()["kitchen_heatmap_enabled"] is False
        assert client.get(ENDPOINT, params=params()).status_code == 404


def test_print_only_route_returns_exact_net_revenue_and_first_orders(settings, monkeypatch):
    monkeypatch.delenv("RESTCONTROL_RUNTIME_MODE", raising=False)
    repo = KitchenRepository()
    repo.restricted = True
    calls = []
    day2 = DAY + timedelta(days=1)

    def fetch(_settings, bodies, *, validate_source, consume):
        assert validate_source is not None
        for body in bodies:
            filters = body["filters"]
            day = date.fromisoformat(filters["OpenDate.Typed"]["from"][:10])
            calls.append(day)
            assert body["buildSummary"] is False
            assert body["aggregateFields"] == ["DishAmountInt", "DishDiscountSumInt"]
            assert filters["Department.Id"]["values"] == [str(DEPARTMENT)]
            assert filters["Store.Id"]["values"] == [str(STORE)]
            assert filters["Storned"]["values"] == ["FALSE"]
            assert filters["OpenDate.Typed"]["to"] == f"{day + timedelta(days=1)}T00:00:00"
            assert [
                field
                for field in ("UniqOrderId.Id", "ItemSaleEvent.Id")
                if field in body["groupByRowFields"]
            ] == ["UniqOrderId.Id", "ItemSaleEvent.Id"]
            if day == DAY:
                consume(
                    [
                        row(day, 1),
                        row(day, 2, **{"DishDiscountSumInt": "-0.19"}),
                        row(day, 3, **{"CookingPlace.Id": None}),
                        row(
                            day,
                            4,
                            **{
                                "DishServicePrintTime": f"{day}T12:15:30Z",
                                "DeletedWithWriteoff": "DELETED_WITH_WRITEOFF",
                            },
                        ),
                        row(
                            day,
                            5,
                            **{
                                "OrderDeleted": "DELETED",
                            },
                        ),
                        row(day, 1, **{"Storned": "TRUE"}),
                        row(day, 1, **{"DishAmountInt": "-1.5"}),
                        row(day, 6, **{"ItemSaleEvent.Id": None, "DishDiscountSumInt": "0.00"}),
                    ],
                    body,
                )
            else:
                consume([row(day, 7, **{"DishAmountInt": 0.5})], body)

    with open_portal(settings, repo, fetch) as client:
        login(client)
        response = client.get(ENDPOINT, params=params(end=day2))
        assert response.status_code == 200, response.text
        payload = response.json()
    assert calls == [DAY, day2]
    assert payload["date_from"] == str(DAY)
    assert payload["date_to"] == str(day2)
    assert payload["department_id"] == str(DEPARTMENT)
    assert set(payload) == {"department_id", "date_from", "date_to", "sent_to_kitchen"}
    sent_hours = [0] * 24
    sent_hours[15] = 1.5
    sent_hours[23] = 6.5
    revenue = [0] * 24
    revenue[23] = 20019
    assert payload["sent_to_kitchen"] == {
        "stations": [{
            "id": str(STATION), "name": "Горячий цех", "hours": sent_hours,
            "revenue_kopecks": revenue,
            "first_print_minutes": {str(DAY): 915, str(day2): 1438},
        }],
        "unassigned_portions": 1.5,
        "unprinted_portions": 0,
        "unassigned_revenue_kopecks": 10019,
        "revenue_complete": True,
        "has_active_print": True,
    }


def test_full_week_accepts_older_dates_and_distinct_groups_of_same_item(settings, monkeypatch):
    monkeypatch.delenv("RESTCONTROL_RUNTIME_MODE", raising=False)
    start = date(1999, 12, 28)
    end = start + timedelta(days=6)
    second_station = str(UUID(int=56))
    requested = []

    def fetch(_settings, bodies, *, validate_source, consume):
        for body in bodies:
            filters = body["filters"]
            day = date.fromisoformat(filters["OpenDate.Typed"]["from"][:10])
            requested.append(day)
            assert "Store.Id" not in filters
            groups = []
            if day == start:
                groups = [
                    row(day, 1, **{"DishAmountInt": 1}),
                    row(
                        day,
                        1,
                        **{"CookingPlace.Id": second_station, "CookingPlace": None},
                    ),
                ]
            consume(groups, body)

    with open_portal(settings, KitchenRepository(), fetch) as client:
        login(client)
        response = client.get(ENDPOINT, params=params(start=start, end=end))
        assert response.status_code == 200, response.text
    assert requested == [start + timedelta(days=offset) for offset in range(7)]
    station_rows = response.json()["sent_to_kitchen"]["stations"]
    stations = {station["id"]: station for station in station_rows}
    assert stations[str(STATION)]["hours"][23] == 1
    assert stations[second_station]["hours"][23] == 1.5
    assert stations[second_station]["name"] == second_station


@pytest.mark.parametrize("printed", [None, "0001-01-01T00:00:00", "1970-01-01T00:00:00",
                                     "2026-09-10", "2026-09-10T99:15:00"])
def test_missing_or_invalid_print_has_no_instant(printed):
    assert local_instant(printed) is None


@pytest.mark.parametrize("bad_amount", [None, True, "NaN", "0.001", "90071992547409.92"])
def test_invalid_money_does_not_erase_portions(bad_amount):
    totals = HeatmapTotals(DEPARTMENT, None, now=datetime(2026, 9, 11, tzinfo=UTC))
    totals.consume(
        [row(DAY, 1, DishDiscountSumInt=bad_amount), row(DAY, 2)],
        daily_request(DEPARTMENT, None, DAY),
    )
    sent = totals.response(DAY, DAY).sent_to_kitchen
    assert not sent.revenue_complete
    assert sent.stations[0].hours[23] == 3
    assert sent.stations[0].revenue_kopecks[23] == 10019


def test_money_above_safe_bucket_limit_is_incomplete():
    totals = HeatmapTotals(DEPARTMENT, None, now=datetime(2026, 9, 11, tzinfo=UTC))
    totals.consume(
        [row(DAY, 1, DishDiscountSumInt="90071992547409.91"), row(DAY, 2)],
        daily_request(DEPARTMENT, None, DAY),
    )
    sent = totals.response(DAY, DAY).sent_to_kitchen
    assert not sent.revenue_complete
    assert sent.stations[0].hours[23] == 3
    assert sent.stations[0].revenue_kopecks[23] == 0


def test_first_print_uses_signed_business_day_offset_and_earliest_local_time():
    totals = HeatmapTotals(DEPARTMENT, None, now=datetime(2026, 9, 11, tzinfo=UTC))
    totals.consume(
        [
            row(DAY, 1, DishServicePrintTime="2026-09-10T00:00:00"),
            row(DAY, 2, DishServicePrintTime="2026-09-09T20:58:59Z"),
        ],
        daily_request(DEPARTMENT, None, DAY),
    )
    station = totals.response(DAY, DAY).sent_to_kitchen.stations[0]
    assert station.first_print_minutes == {DAY: -2}
    assert station.hours[0] == station.hours[23] == 1.5


def test_sent_to_kitchen_uses_only_observed_print_and_keeps_station_ids_distinct():
    now = datetime(2026, 9, 10, 21, 15, tzinfo=UTC)
    totals = HeatmapTotals(DEPARTMENT, None, now=now)
    body = daily_request(DEPARTMENT, None, DAY)
    second_station = str(UUID(int=56))
    totals.consume(
        [
            row(
                DAY,
                1,
                **{
                    "DishServicePrintTime": f"{DAY}T21:10:00Z",
                    "DishAmountInt": "0.25",
                },
            ),
            row(
                DAY,
                2,
                **{
                    "DishServicePrintTime": "2026-09-11T00:15:00",
                    "CookingPlace.Id": second_station,
                    "OrderDeleted": "DELETED",
                    "DishAmountInt": "0.75",
                },
            ),
            row(DAY, 3, DishServicePrintTime="2026-09-11T00:15:00.000001", DishDiscountSumInt=None),
            row(DAY, 4, **{"DishServicePrintTime": "2026-09-10T99:15:00"}),
            row(DAY, 5, **{"DishServicePrintTime": None, "OrderDeleted": "DELETED"}),
            row(DAY, 6, **{"DishServicePrintTime": None, "CookingPlace.Id": None}),
        ],
        body,
    )
    response = totals.response(DAY, DAY)
    sent = response.sent_to_kitchen
    assert sent.unprinted_portions == 4.5
    assert sent.unassigned_portions == 0
    assert [(station.id, station.name, station.hours) for station in sent.stations] == [
        (str(STATION), "Горячий цех", [0.25, *([0] * 23)]),
        (second_station, "Горячий цех", [0.75, *([0] * 23)]),
    ]

    assert sent.revenue_complete
    assert sent.stations[0].first_print_minutes == {DAY: 1450}
    assert sent.stations[1].first_print_minutes == {DAY: 1455}
    assert sent.stations[0].revenue_kopecks[0] == 10019
    assert sent.stations[1].revenue_kopecks[0] == 0


@pytest.mark.parametrize("include_printed", [False, True])
def test_missing_and_null_print_fields_do_not_supply_money_or_time(include_printed):
    totals = HeatmapTotals(DEPARTMENT, None, now=datetime(2026, 9, 11, tzinfo=UTC))
    missing = row(DAY, 1, DishDiscountSumInt="123.45")
    del missing["DishServicePrintTime"]
    rows = [missing, row(DAY, 2, DishServicePrintTime=None, DishDiscountSumInt="67.89")]
    if include_printed:
        rows.append(row(DAY, 3))
    totals.consume(rows, daily_request(DEPARTMENT, None, DAY))
    sent = totals.response(DAY, DAY).sent_to_kitchen
    assert sent.unprinted_portions == 3
    assert sent.unassigned_revenue_kopecks == 0
    assert sent.revenue_complete
    assert sent.has_active_print is include_printed
    if include_printed:
        [station] = sent.stations
        assert sum(station.hours) == 1.5
        assert sum(station.revenue_kopecks) == 10019
        assert station.first_print_minutes == {DAY: 1438}
    else:
        assert not sent.stations


def test_print_presence_and_monetary_integrity_combine_across_requests():
    next_day = DAY + timedelta(days=1)
    now = datetime(2026, 9, 12, tzinfo=UTC)
    report_days = [
        (DAY, [row(DAY, 1, DishServicePrintTime=None)]),
        (next_day, [row(next_day, 2)]),
    ]
    combined = HeatmapTotals(DEPARTMENT, None, now=now)
    chunks = []
    for day, rows in report_days:
        body = daily_request(DEPARTMENT, None, day)
        chunk = HeatmapTotals(DEPARTMENT, None, now=now)
        chunk.consume(rows, body)
        combined.consume(rows, body)
        chunks.append(chunk.response(day, day).sent_to_kitchen)
    sent = combined.response(DAY, next_day).sent_to_kitchen
    assert not chunks[0].has_active_print and chunks[0].unprinted_portions > 0
    assert sent.has_active_print and any(chunk.has_active_print for chunk in chunks)
    assert sent.revenue_complete and all(chunk.revenue_complete for chunk in chunks)
    assert sent.unprinted_portions == sum(chunk.unprinted_portions for chunk in chunks)
    assert sent.stations == chunks[1].stations


def test_missing_daily_report_rejects_partial_heatmap(settings, monkeypatch):
    monkeypatch.delenv("RESTCONTROL_RUNTIME_MODE", raising=False)

    def fetch(_settings, bodies, *, validate_source, consume):
        consume([row(DAY, 1)], bodies[0])

    with open_portal(settings, KitchenRepository(), fetch) as client:
        login(client)
        response = client.get(ENDPOINT, params=params(end=DAY + timedelta(days=1)))
        assert response.status_code == 503


@pytest.mark.parametrize(
    "broken",
    [
        [{"Store.Id": None}],
        [{"Store.Id": str(OTHER)}],
        [{"Department.Id": str(OTHER)}],
        [{"OpenDate.Typed": "2026-09-11"}],
        [{}, {}],
        [{"DishAmountInt": None}],
    ],
)
def test_bad_or_out_of_scope_report_is_explicit_error(settings, monkeypatch, broken):
    monkeypatch.delenv("RESTCONTROL_RUNTIME_MODE", raising=False)
    repo = KitchenRepository()
    repo.restricted = True

    def fetch(_settings, bodies, *, validate_source, consume):
        consume([row(DAY, 1, **overrides) for overrides in broken], bodies[0])

    with open_portal(settings, repo, fetch) as client:
        login(client)
        assert client.get(ENDPOINT, params=params()).status_code == 503


def test_transport_fences_paused_or_mismatched_sources_and_logs_out_on_error(settings, monkeypatch):
    monkeypatch.delenv("RESTCONTROL_RUNTIME_MODE", raising=False)
    policy = SyncPolicy(departments=frozenset())
    fingerprint = "mismatched"
    report = '{"data": "not an array"}'
    events = []

    class Database:
        def execute(self, query):
            assert "chaika.sources" in query
            events.append("verify_source")
            return SimpleNamespace(fetchone=lambda: (fingerprint,))

    class Client:
        def __init__(self, _):
            events.append("client")

        async def aclose(self):
            events.append("closed")

    class Auth:
        def __init__(self, *_):
            pass

        async def download_daily_sales(self, path, *, query):
            events.append("report")
            path.write_text(report)

        async def logout(self):
            events.append("logout")
            return SimpleNamespace(state="logged_out")

    def private_logout(*_, **__):
        events.append("private_logout")
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://127.0.0.1"),
            json={
                "state": "logged_out",
            },
        )

    monkeypatch.setattr(sync_policy, "read_policy", lambda _: policy)
    monkeypatch.setattr(indicators.psycopg, "connect", lambda *a, **k: nullcontext(Database()))
    monkeypatch.setattr(indicators, "reference_lock", lambda _: nullcontext())
    monkeypatch.setattr(indicators.httpx, "post", private_logout)
    monkeypatch.setattr(indicators, "IikoClient", Client)
    monkeypatch.setattr(indicators, "IikoAuthService", Auth)

    with open_portal(settings, KitchenRepository()) as client:
        login(client)
        assert client.get(ENDPOINT, params=params()).status_code == 423
        assert events == []
        policy = SyncPolicy(departments=frozenset([str(DEPARTMENT)]))
        assert client.get(ENDPOINT, params=params()).status_code == 503
        assert events == ["verify_source"]
        identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
        fingerprint = sha256(identity.encode()).hexdigest()
        assert client.get(ENDPOINT, params=params()).status_code == 503
        for raw in ('{"data":[]}', '{"data":[],"summary":null}'):
            report = raw
            empty = client.get(ENDPOINT, params=params())
            assert empty.status_code == 200, empty.text
            assert empty.json()["sent_to_kitchen"] == {
                "stations": [],
                "unassigned_portions": 0,
                "unprinted_portions": 0,
                "unassigned_revenue_kopecks": 0,
                "revenue_complete": True,
                "has_active_print": False,
            }
        report = '{"data":[],"summary":[{"DishAmountInt":42}]}'
        assert client.get(ENDPOINT, params=params()).status_code == 503
    assert events[:1] == ["verify_source"]
    assert (
        events[1:]
        == [
            "verify_source",
            "private_logout",
            "client",
            "report",
            "logout",
            "closed",
        ]
        * 4
    )
