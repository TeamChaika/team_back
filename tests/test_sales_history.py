import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from test_sales_import import review as review

from app import sync_sales_history as history
from app.import_sales_review import KINDS, parse_review
from app.integrations.iiko.errors import IikoError
from app.sync_references import SyncError


def days(count=3):
    return [date(2026, 8, 1) + timedelta(days=i) for i in range(count)]


def result():
    return {"status": "imported", "rows": {kind: 1 for kind in KINDS}}


def test_resume_only_requests_missing_days_and_counts_committed_reports():
    calls, checkpoints = [], []

    def sync(day):
        calls.append(day)
        return result()

    report = history.run_history(
        days(),
        {days()[1]: result()["rows"]},
        sync,
        lambda report: checkpoints.append(deepcopy(report)),
        Event(),
    )
    assert calls == [days()[0], days()[2]]
    assert report["status"] == "succeeded"
    assert report["counts"]["completed_days"] == 3
    assert report["counts"]["documents_read"] == 21
    assert report["counts"]["resumed_days"] == 1
    assert checkpoints[0]["counts"]["completed_through"] is None
    assert report["counts"]["next_date"] is None


@pytest.mark.parametrize("failure", [ValueError("private password"), SyncError("sales_failed")])
def test_failed_day_is_not_counted_and_can_be_resumed_without_reloading_prior_days(failure):
    def sync(day):
        if day == days()[1]:
            raise failure
        return result()

    report = history.run_history(days(), {}, sync, lambda _: None, Event())
    assert report["status"] == "failed"
    assert report["counts"]["completed_days"] == 1
    assert report["counts"]["next_date"] == "2026-08-02"
    assert "private" not in json.dumps(report)


def test_stop_after_commit_preserves_the_day():
    stop = Event()

    def sync(day):
        stop.set()
        return result()

    report = history.run_history(days(), {}, sync, lambda _: None, stop)
    assert report["status"] == "interrupted"
    assert report["counts"]["completed_days"] == 1


def test_partial_result_cannot_advance_coverage():
    report = history.run_history(
        days(),
        {},
        lambda _: {"status": "imported", "rows": {"daily": 4}},
        lambda _: None,
        Event(),
    )
    assert report["status"] == "failed"
    assert report["counts"]["completed_days"] == 0


@pytest.mark.parametrize(
    "start,end",
    [
        (date(1999, 1, 1), date(2026, 8, 1)),
        (date(2026, 8, 2), date(2026, 8, 1)),
        (date(2026, 8, 1), date(9999, 1, 1)),
    ],
)
def test_invalid_or_open_period_rejected(start, end):
    with pytest.raises(SyncError, match="invalid_sales_history_period"):
        history.history_days(start, end)


def test_new_date_preserves_all_approved_groupings_and_filters(review):
    _, manifest = review
    template = manifest["reports"]["hours"]["request"]
    saved = deepcopy(template)
    request = history.request_for_day(template, date(2023, 3, 1)).iiko_body()
    assert template == saved
    assert request["groupByRowFields"] == template["groupByRowFields"]
    assert request["filters"]["OpenDate.Typed"]["to"] == "2023-03-02T00:00:00"
    assert request["buildSummary"] is False


def fake_collection(monkeypatch, review, failure=None, logout_failure=False):
    original, manifest = review
    calls = []
    templates = {kind: info["request"] for kind, info in manifest["reports"].items()}

    class Client:
        def __init__(self, settings):
            pass

        async def aclose(self):
            calls.append("closed")

    class Auth:
        def __init__(self, settings, client):
            pass

        async def download_daily_sales(self, path, *, query):
            calls.append(path.stem)
            if failure and path.stem == "payments":
                raise IikoError("iiko_test_failure", "private")
            raw = (original / path.name).read_bytes()
            path.write_bytes(raw)
            return SimpleNamespace(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))

        async def logout(self):
            calls.append("logout")
            if logout_failure:
                raise IikoError("iiko_logout_failed", "private")
            return SimpleNamespace(state="logged_out")

    monkeypatch.setattr(history, "IikoClient", Client)
    monkeypatch.setattr(history, "IikoAuthService", Auth)
    return templates, calls


def test_collection_preserves_sequential_raw_and_confirmed_logout(review, monkeypatch):
    templates, calls = fake_collection(monkeypatch, review)
    root = review[0] / "capture"
    asyncio.run(
        history.collect_day(
            None,
            SimpleNamespace(fingerprint="test-source"),
            date(2026, 9, 10),
            templates,
            "approved",
            root,
            Event(),
        )
    )
    assert calls == [*history.REPORT_ORDER, "logout", "closed"]
    assert set(parse_review(root, "test-source")["reports"]) == KINDS


@pytest.mark.parametrize("logout_failure", [False, True])
def test_failed_collection_still_logs_out_and_retains_raw(review, monkeypatch, logout_failure):
    templates, calls = fake_collection(
        monkeypatch, review, failure=True, logout_failure=logout_failure
    )
    root = review[0] / "capture"
    with pytest.raises((IikoError, SyncError)):
        asyncio.run(
            history.collect_day(
                None,
                SimpleNamespace(fingerprint="test-source"),
                date(2026, 9, 10),
                templates,
                "approved",
                root,
                Event(),
            )
        )
    assert calls == ["daily", "dishes", "payments", "logout", "closed"]
    assert (root / "raw/daily.json").exists()
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["errors"]
    assert "private" not in json.dumps(manifest)
    with pytest.raises(ValueError):
        parse_review(root, "test-source")


def test_today_requires_explicit_opt_in_and_future_stays_rejected():
    today = history.datetime.now(history.ZONE).date()
    with pytest.raises(SyncError, match="invalid_sales_history_period"):
        history.history_days(today, today)
    assert history.history_days(today, today, include_today=True) == [today]
    with pytest.raises(SyncError, match="invalid_sales_history_period"):
        history.history_days(today, today + timedelta(days=1), include_today=True)
