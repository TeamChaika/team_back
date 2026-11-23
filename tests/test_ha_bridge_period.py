"""Calendar-day selection is server-owned; all fixtures are synthetic and I/O-free."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from test_ha_bridge import HEADERS, PATH
from test_ha_bridge import setup as setup

import app.web.ha_bridge as bridge
import app.web.live_sales as live_module
from app.web.coverage import ZONE


@pytest.fixture
def period_setup(setup, monkeypatch):
    monkeypatch.setattr(live_module, "datetime", bridge.datetime)
    setup.production_fetch_day = live_module.fetch_day
    calls = []
    setup.state.after_history = None

    def capture(settings, day):
        calls.append(day)
        if setup.state.failing:
            raise HTTPException(503, "synthetic provider unavailable")
        data = deepcopy(setup.data)
        data["manifest"]["business_date"] = day.isoformat()
        for report in data["reports"].values():
            report["info"]["received_at"] = setup.wall[0].isoformat()
            for row in report["rows"]:
                row["OpenDate.Typed"] = day.isoformat()
        if setup.state.after_history:
            setup.state.after_history()
        return data

    monkeypatch.setattr(live_module, "fetch_day", capture, raising=False)
    setup.history_calls = calls

    def move(iso):
        setup.wall[0] = datetime.fromisoformat(iso).astimezone(UTC)
        today = setup.wall[0].astimezone(ZONE).date().isoformat()
        setup.data["manifest"]["business_date"] = today
        for report in setup.data["reports"].values():
            report["info"]["received_at"] = setup.wall[0].isoformat()
            for row in report["rows"]:
                row["OpenDate.Typed"] = today

    setup.move = move
    return setup


@pytest.mark.parametrize(
    ("at", "expected", "partial"),
    [
        ("2026-11-23T00:00:00+03:00", "2026-11-22", False),
        ("2026-11-23T08:59:59+03:00", "2026-11-22", False),
        ("2026-11-23T09:00:00+03:00", "2026-11-23", True),
        ("2026-11-23T23:59:59+03:00", "2026-11-23", True),
        ("2026-12-01T00:00:00+03:00", "2026-11-30", False),
        ("2027-01-01T08:59:59+03:00", "2026-12-31", False),
        ("2028-03-01T08:59:59+03:00", "2028-02-29", False),
        ("2026-11-22T21:00:00+00:00", "2026-11-22", False),
    ],
)
def test_selected_date_and_real_snapshot_match_calendar_rule(period_setup, at, expected, partial):
    period_setup.move(at)
    with period_setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["period"]["business_date"] == expected
    assert result["period"]["start"] == expected + "T00:00:00+03:00"
    assert result["period"]["partial"] is partial
    assert result["revenue"] == "150.50"
    assert result["is_fresh"] is True
    assert result["coverage"]["daily_present"] is True
    if not partial:
        assert [day.isoformat() for day in period_setup.history_calls] == [expected]


def test_yesterday_cache_is_separate_from_other_dashboards_today_cache(period_setup):
    period_setup.move("2026-11-23T08:59:00+03:00")
    with period_setup.make() as client:
        yesterday = client.get(PATH, headers=HEADERS)
        assert yesterday.json()["period"]["business_date"] == "2026-11-22"
        assert period_setup.live.get()[0]["manifest"]["business_date"] == "2026-11-23"
        period_setup.move("2026-11-23T09:00:00+03:00")
        today = client.get(PATH, headers=HEADERS)
    assert today.json()["period"]["business_date"] == "2026-11-23"
    assert len(period_setup.history_calls) == 1


def test_failed_yesterday_capture_never_falls_back_to_today_values(period_setup):
    period_setup.move("2026-11-23T03:00:00+03:00")
    with period_setup.make() as client:
        period_setup.state.failing = True
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["period"]["business_date"] == "2026-11-22"
    assert response.json()["revenue"] is None


def test_yesterday_cache_expires_normally_and_remains_date_bound(period_setup):
    period_setup.move("2026-11-23T03:00:00+03:00")
    with period_setup.make() as client:
        first = client.get(PATH, headers=HEADERS).json()
        period_setup.ticks[0] = 100
        assert client.get(PATH, headers=HEADERS).json()["last_good"] == first["last_good"]
        assert len(period_setup.history_calls) == 1
        period_setup.state.failing = True
        period_setup.ticks[0] = 300
        stale = client.get(PATH, headers=HEADERS)
        assert stale.status_code == 200
        assert stale.json()["period"]["business_date"] == "2026-11-22"
        assert stale.json()["is_fresh"] is False
        period_setup.ticks[0] = 1200
        expired = client.get(PATH, headers=HEADERS)
    assert expired.status_code == 503
    assert expired.json()["revenue"] is None


def test_nine_oclock_cutoff_during_capture_hides_previous_day(period_setup):
    period_setup.move("2026-11-23T08:59:59+03:00")
    with period_setup.make() as client:
        period_setup.state.after_history = lambda: period_setup.move("2026-11-23T09:00:00+03:00")
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["period"]["business_date"] == "2026-11-23"
    assert response.json()["revenue"] is None


def test_new_midnight_never_reuses_pre_midnight_capture_as_final_yesterday(period_setup):
    period_setup.move("2026-11-22T23:59:59+03:00")
    with period_setup.make() as client:
        assert client.get(PATH, headers=HEADERS).status_code == 200
        period_setup.move("2026-11-23T00:00:00+03:00")
        yesterday = client.get(PATH, headers=HEADERS)
    assert yesterday.status_code == 200
    assert yesterday.json()["period"]["business_date"] == "2026-11-22"
    assert yesterday.json()["period"]["partial"] is False
    assert yesterday.json()["last_good"] == "2026-11-22T21:00:00+00:00"
    assert len(period_setup.history_calls) == 1


def test_date_override_stays_rejected_before_nine(period_setup):
    period_setup.move("2026-11-23T03:00:00+03:00")
    with period_setup.make() as client:
        response = client.get(PATH + "?day=2026-11-23", headers=HEADERS)
    assert response.status_code == 422


def test_yesterday_capture_received_before_midnight_is_not_a_final_day(period_setup, monkeypatch):
    period_setup.move("2026-11-23T00:00:00+03:00")

    def old_observation(settings, day):
        data = deepcopy(period_setup.data)
        data["manifest"]["business_date"] = day.isoformat()
        for report in data["reports"].values():
            report["info"]["received_at"] = (
                period_setup.wall[0] - timedelta(seconds=1)
            ).isoformat()
            for row in report["rows"]:
                row["OpenDate.Typed"] = day.isoformat()
        return data

    monkeypatch.setattr(live_module, "fetch_day", old_observation)
    with period_setup.make() as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    result = response.json()
    assert result["period"]["business_date"] == "2026-11-22"
    assert result["period"]["partial"] is False
    assert result["revenue"] is None and result["is_fresh"] is False


def test_next_calendar_day_gets_a_different_immutable_yesterday_snapshot(period_setup):
    period_setup.move("2026-11-23T03:00:00+03:00")
    with period_setup.make() as client:
        first = client.get(PATH, headers=HEADERS).json()
        period_setup.move("2026-11-24T03:00:00+03:00")
        second = client.get(PATH, headers=HEADERS).json()
    assert first["period"]["business_date"] == "2026-11-22"
    assert second["period"]["business_date"] == "2026-11-23"
    assert [day.isoformat() for day in period_setup.history_calls] == ["2026-11-22", "2026-11-23"]


def test_real_fetch_day_passes_immutable_date_through_original_mutex(period_setup, monkeypatch):
    period_setup.move("2026-11-23T08:59:59+03:00")
    selected = datetime(2026, 11, 22).date()
    gate, evidence = {"held": False}, []
    policy = period_setup.repo.policy
    connection = SimpleNamespace()

    @contextmanager
    def database(*args, **kwargs):
        yield connection

    @contextmanager
    def mutex(db):
        assert db is connection
        gate["held"] = True
        try:
            yield
        finally:
            gate["held"] = False

    def collector_logout(*args, **kwargs):
        assert gate["held"]
        return httpx.Response(
            200,
            json={"state": "logged_out"},
            request=httpx.Request("POST", "http://127.0.0.1/test-only"),
        )

    async def collect(settings, source, day, templates, template_id, root, stop):
        evidence.append((day.isoformat(), gate["held"]))
        period_setup.wall[0] += timedelta(days=1)

    def parse(*args, **kwargs):
        assert gate["held"]
        return {"manifest": {"business_date": "2026-11-22"}, "reports": {}}

    monkeypatch.setattr(live_module.psycopg, "connect", database)
    monkeypatch.setattr(live_module, "reference_lock", mutex)
    monkeypatch.setattr(
        live_module, "configured_sources", lambda _: [SimpleNamespace(fingerprint="test-only")]
    )
    monkeypatch.setattr(live_module, "read_policy", lambda _: policy)
    monkeypatch.setattr(live_module, "approved_templates", lambda _: ("test-only", {}))
    monkeypatch.setattr(live_module.httpx, "post", collector_logout)
    monkeypatch.setattr(live_module, "collect_day", collect)
    monkeypatch.setattr(live_module, "parse_review", parse)
    # Restore the production function: the period fixture otherwise replaces ERP I/O.
    original = period_setup.production_fetch_day
    result = original(period_setup.live.settings, selected)
    assert evidence == [("2026-11-22", True)]
    assert result["manifest"]["business_date"] == "2026-11-22"
    assert result["sync_policy_key"] == policy.key
    assert gate["held"] is False
