"""Synthetic HTTP contracts; real scope, LiveSales and Repository aggregation, no external I/O."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.portal as portal
import app.web.ha_bridge as bridge
from app.core.config import Settings
from app.web.auth import ACCESS_COOKIE
from app.web.coverage import ZONE
from app.web.live_sales import LiveSales
from app.web.repository import Repository
from app.web.settings import WebSettings
from app.web.sync_policy import SyncPolicy

USER, VENUE, OTHER, STORE = (UUID(int=i) for i in (101, 102, 103, 104))
TOKEN = "synthetic-test-only-ha-bearer-0123456789"
PATH = "/api/integrations/home-assistant/today"
HEADERS = {"Authorization": "Bearer " + TOKEN}


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return deepcopy(self.rows)

    def fetchone(self):
        return deepcopy(self.rows[0]) if self.rows else None


class MemoryRepository(Repository):
    """Only PostgreSQL I/O is substituted; production permission and report code runs."""

    def __init__(self, settings):
        self.settings = settings
        self.user = {
            "id": USER,
            "display_name": "Synthetic owner",
            "role": "owner",
            "active": True,
            "all_departments": True,
            "sections": ["sales"],
            "is_portal_admin": True,
            "password_change_required": False,
            "warehouse_scope_mode": "all",
        }
        self.grants = [VENUE]
        self.policy = SyncPolicy()

    def sync_policy(self):
        return self.policy

    @contextmanager
    def connection(self, **kwargs):
        yield self

    def execute(self, sql, args=()):
        if "FROM chaika.web_users WHERE id=%s AND active" in sql:
            return Cursor([self.user] if args[0] == USER and self.user["active"] else [])
        if "FROM chaika.corporate_nodes" in sql:
            return Cursor(
                [
                    {
                        "id": VENUE,
                        "parent_id": None,
                        "name": "Pilot",
                        "code": "1",
                        "type": "DEPARTMENT",
                    },
                    {
                        "id": OTHER,
                        "parent_id": None,
                        "name": "Hidden",
                        "code": "2",
                        "type": "DEPARTMENT",
                    },
                ]
            )
        if "FROM chaika.web_department_access" in sql:
            return Cursor([{"department_id": value} for value in self.grants])
        if "FROM chaika.stores" in sql:
            return Cursor([{"id": STORE, "parent_id": VENUE, "name": "Pilot store"}])
        if "FROM chaika.rms_bindings" in sql or "FROM chaika.web_warehouse_access" in sql:
            return Cursor([])
        if "FROM chaika.sales_report_days" in sql:
            return Cursor([])  # Today's live snapshot must replace absent stored rows.
        raise AssertionError("Unexpected test SQL")


def sample():
    day = datetime.now(ZONE).date().isoformat()
    observed = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()

    def row(venue, revenue, checks, **dimensions):
        return {
            "Department.Id": str(venue),
            "OpenDate.Typed": day,
            "DishDiscountSumInt": revenue,
            "UniqOrderId": checks,
            "ProductCostBase.ProductCost": "1",
            "GuestNum": "3",
            **dimensions,
        }

    daily = [row(VENUE, "150.50", "3"), row(OTHER, "99999.99", "999")]
    hours = [
        row(VENUE, "100.25", "2", HourOpen="9"),
        row(VENUE, "50.25", "1", HourOpen="10"),
        row(OTHER, "99999.99", "999", HourOpen="9"),
    ]
    return {
        "id": UUID(int=105),
        "manifest": {"business_date": day},
        "checks": [],
        "reports": {
            kind: {
                "rows": rows,
                "info": {
                    "received_at": observed,
                    "request": {
                        "groupByRowFields": ["OpenDate.Typed", "Department.Id"]
                        + (["HourOpen"] if kind == "hours" else []),
                    },
                },
            }
            for kind, rows in (("daily", daily), ("hours", hours))
        },
    }


@pytest.fixture
def setup(monkeypatch):
    wall = [datetime.now(UTC).astimezone(ZONE).replace(hour=12, minute=0, second=0).astimezone(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0].astimezone(tz)

    monkeypatch.setattr(bridge, "datetime", Clock)
    settings = Settings(_env_file=None, live_sales_enabled=True)
    repo = MemoryRepository(settings)
    data, ticks = sample(), [0.0]
    for report in data["reports"].values():
        report["info"]["received_at"] = (wall[0] - timedelta(seconds=2)).isoformat()
    state = SimpleNamespace(failing=False, after_fetch=None, fetches=0)

    def fetch(_):
        state.fetches += 1
        if state.failing:
            raise HTTPException(503, "synthetic provider unavailable")
        if state.after_fetch:
            state.after_fetch()
        return deepcopy(data)

    live = LiveSales(settings, fetch=fetch, clock=lambda: ticks[0], policy_reader=repo.sync_policy)
    monkeypatch.setattr(portal, "LiveSales", lambda *args, **kwargs: live)

    def make(**options):
        web = WebSettings(
            _env_file=None,
            **{
                "ha_bridge_enabled": True,
                "ha_bridge_token_sha256": sha256(TOKEN.encode()).hexdigest(),
                "ha_bridge_user_id": USER,
                "ha_bridge_department_id": VENUE,
                **options,
            },
        )

        def auth(request):
            if request.headers.get("authorization") == "Bearer browser-test-cookie":
                return httpx.Response(200, json={"id": str(USER)})
            return httpx.Response(401)

        app = portal.create_portal(
            settings, web, repository=repo, auth_transport=httpx.MockTransport(auth)
        )
        return TestClient(app)

    return SimpleNamespace(
        make=make, repo=repo, data=data, ticks=ticks, state=state, live=live, wall=wall, now=wall[0]
    )


def test_disabled_by_default_is_not_a_new_public_route(setup):
    app = portal.create_portal(
        Settings(_env_file=None), WebSettings(_env_file=None), repository=setup.repo
    )
    with TestClient(app) as client:
        assert client.get(PATH, headers=HEADERS).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"X-Sync-Key": TOKEN},
        {"Authorization": "Basic " + TOKEN},
    ],
)
def test_only_dedicated_bearer_can_read_feed(setup, headers):
    with setup.make() as client:
        client.cookies.set(ACCESS_COOKIE, "browser-test-cookie")
        assert client.get(PATH, headers=headers).status_code == 401


def test_owner_token_is_fixed_to_one_venue_and_aggregate_fields(setup):
    with setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["restaurant"] == {"id": str(VENUE), "name": "Pilot"}
    assert result["revenue"] == "150.50"
    assert result["checks"] == 3
    assert Decimal(result["average_check"]).quantize(Decimal("0.01")) == Decimal("50.17")
    assert result["hourly"][9] == {"hour": 9, "revenue": "100.25", "checks": 2}
    assert result["hourly"][10] == {"hour": 10, "revenue": "50.25", "checks": 1}
    assert result["hourly"][8] == {"hour": 8, "revenue": None, "checks": None}
    assert result["source"]["hour_field"] == "HourOpen"
    assert result["is_fresh"] is True
    assert result["completeness"] == "complete"
    assert result["period"]["business_date"] == datetime.now(ZONE).date().isoformat()
    assert result["period"]["partial"] is True
    assert result["last_good"] is not None
    assert 0 <= result["cache_age_seconds"] < 30
    assert response.headers["Cache-Control"] == "no-store"
    assert "99999" not in response.text
    assert set(result) == {
        "restaurant",
        "period",
        "source",
        "revenue",
        "checks",
        "average_check",
        "hourly",
        "fetched_at",
        "last_good",
        "cache_age_seconds",
        "cache_seconds",
        "is_fresh",
        "completeness",
        "coverage",
    }


@pytest.mark.parametrize("query", ["department_id=other", "filters={}", "start=2000-01-01"])
def test_request_cannot_override_scope_or_period(setup, query):
    with setup.make() as client:
        assert client.get(PATH + "?" + query, headers=HEADERS).status_code == 422


@pytest.mark.parametrize(
    "change", ["disabled", "sales_revoked", "venue_revoked", "password_required"]
)
def test_current_profile_permissions_checked_even_when_cache_is_hot(setup, change):
    with setup.make() as client:
        assert client.get(PATH, headers=HEADERS).status_code == 200
        if change == "disabled":
            setup.repo.user["active"] = False
        elif change == "sales_revoked":
            setup.repo.user["sections"] = []
        elif change == "venue_revoked":
            setup.repo.user["all_departments"] = False
            setup.repo.grants = [OTHER]
        else:
            setup.repo.user["password_change_required"] = True
        assert client.get(PATH, headers=HEADERS).status_code == 403


def test_permissions_revoked_during_provider_refresh_are_rechecked(setup):
    with setup.make() as client:
        setup.ticks[0] = 300
        setup.state.after_fetch = lambda: setup.repo.user.update(active=False)
        assert client.get(PATH, headers=HEADERS).status_code == 403


def test_bearer_does_not_unlock_existing_cookie_routes_or_writes(setup):
    with setup.make() as client:
        assert client.get("/api/me", headers=HEADERS).status_code == 401
        assert (
            client.get(
                "/api/sales/daily",
                headers=HEADERS,
                params={"start": "2000-01-02", "end": "2000-01-02"},
            ).status_code
            == 401
        )
        assert client.post(PATH, headers=HEADERS).status_code == 405


def test_no_rows_means_null_not_zero(setup):
    for report in setup.data["reports"].values():
        report["rows"] = []
    with setup.make() as client:
        result = client.get(PATH, headers=HEADERS).json()
    assert result["revenue"] is None and result["checks"] is None
    assert result["average_check"] is None
    assert result["last_good"] is None
    assert all(row["revenue"] is None for row in result["hourly"])
    assert result["completeness"] == "incomplete"
    assert result["is_fresh"] is False


@pytest.mark.parametrize("missing", ["daily", "hours", "mismatch", "null_metric"])
def test_incomplete_snapshot_never_exposes_partial_kpis_or_hourly_values(setup, missing):
    if missing in {"daily", "hours"}:
        setup.data["reports"][missing]["rows"] = []
    elif missing == "mismatch":
        setup.data["reports"]["hours"]["rows"][0]["DishDiscountSumInt"] = "1.00"
    else:
        setup.data["reports"]["daily"]["rows"][0]["UniqOrderId"] = None
    with setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["completeness"] == "incomplete"
    assert result["is_fresh"] is False and result["last_good"] is None
    assert result["revenue"] is None and result["checks"] is None
    assert result["average_check"] is None
    assert all(row["revenue"] is None and row["checks"] is None for row in result["hourly"])
    assert result["coverage"]["reconciled"] is False
    assert result["coverage"]["observed_at"] is not None


def test_provider_zero_is_preserved_and_zero_checks_have_null_average(setup):
    for kind, report in setup.data["reports"].items():
        report["rows"] = [
            {
                "Department.Id": str(VENUE),
                "DishDiscountSumInt": "0.00",
                "OpenDate.Typed": setup.data["manifest"]["business_date"],
                "UniqOrderId": "0",
                **({"HourOpen": "9"} if kind == "hours" else {}),
            }
        ]
    with setup.make() as client:
        result = client.get(PATH, headers=HEADERS).json()
    assert result["revenue"] == "0.00" and result["checks"] == 0
    assert result["average_check"] is None
    assert result["hourly"][9]["revenue"] == "0.00"


def test_provider_failure_keeps_bounded_last_good_as_explicitly_stale(setup):
    with setup.make() as client:
        first = client.get(PATH, headers=HEADERS).json()
        setup.state.failing = True
        setup.ticks[0] = 300
        response = client.get(PATH, headers=HEADERS)
        stale = response.json()
        assert response.status_code == 200
        assert stale["revenue"] == "150.50"
        assert stale["last_good"] == first["last_good"]
        assert stale["is_fresh"] is False
        setup.ticks[0] = 1200
        expired = client.get(PATH, headers=HEADERS)
        assert expired.status_code == 503
        assert expired.json()["revenue"] is None
        assert expired.json()["completeness"] == "unavailable"


def test_yesterdays_snapshot_is_never_returned_as_today(setup):
    setup.data["manifest"]["business_date"] = (
        datetime.now(ZONE).date() - timedelta(days=1)
    ).isoformat()
    with setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["revenue"] is None


def test_sync_paused_is_not_reported_as_live_zero(setup):
    setup.repo.policy = SyncPolicy(departments=frozenset())
    with setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["checks"] is None


def test_missing_configuration_fails_closed(setup):
    with setup.make(ha_bridge_token_sha256="", ha_bridge_department_id=None) as client:
        assert client.get(PATH, headers=HEADERS).status_code == 503


def test_malformed_hour_does_not_claim_a_successful_scoped_observation(setup):
    setup.data["reports"]["hours"]["rows"][0]["HourOpen"] = "99"
    with setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["last_good"] is None


@pytest.mark.parametrize("advance", [timedelta(days=1), timedelta(seconds=1200)])
def test_date_and_age_rechecked_after_final_permission_read(setup, monkeypatch, advance):
    import app.web.ha_bridge as bridge

    wall = [setup.now]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0].astimezone(tz)

    monkeypatch.setattr(bridge, "datetime", Clock)
    original_scope = setup.repo.scope
    calls = [0]

    def scope(*args):
        value = original_scope(*args)
        calls[0] += 1
        if calls[0] == 2:
            wall[0] += advance
        return value

    setup.repo.scope = scope
    with setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["revenue"] is None
    assert response.json()["is_fresh"] is False
