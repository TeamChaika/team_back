"""History orchestration: resume committed dates, never hide holes or partial days."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from threading import Event

import pytest

from app import progress
from app.sync_event_history import completed_coverage, dates_between, run_history, write_json
from app.sync_references import Source, SyncError

START = date(2026, 8, 1)
DAYS = [START + timedelta(days=i) for i in range(3)]
SOURCES = [Source(s, s, f"https://{s}.example/resto/api", "a" * 64) for s in ("one", "two")]


def result(source, day):
    return {
        "status": "succeeded",
        "run_id": "test",
        "source_id": source,
        "date": str(day),
        "counts": {"events": 12, "transfer_events_matched": 2},
    }


def test_resume_fills_holes_and_interleaves_without_reloading_committed_days():
    calls, reports = [], []
    coverage = {"one": {DAYS[1]: 15}, "two": {}}

    def sync(source, day):
        calls.append((source, day))
        return result(source, day)

    outcome = run_history(
        SOURCES, DAYS, coverage, sync, lambda r: reports.append(deepcopy(r)), Event()
    )
    assert calls == [
        ("one", DAYS[0]),
        ("two", DAYS[0]),
        ("two", DAYS[1]),
        ("one", DAYS[2]),
        ("two", DAYS[2]),
    ]
    first = reports[0]["sources"][0]["counts"]
    assert first["completed_days"] == 1 and first["completed_through"] is None
    assert first["next_date"] == str(START)
    assert outcome["status"] == "succeeded"
    assert outcome["sources"][0]["counts"]["events_read"] == 39


def test_partial_capture_requires_reload_even_after_day_becomes_historical():
    rows = [
        (DAYS[0], 11, datetime(2026, 8, 1, 20, 59, 59, tzinfo=UTC)),
        (DAYS[1], 22, datetime(2026, 8, 2, 21, 0, 0, tzinfo=UTC)),
    ]
    assert completed_coverage(rows, DAYS) == {DAYS[1]: 22}


def test_fully_resumed_history_keeps_persisted_transfer_counts():
    def unexpected_request(*_):
        pytest.fail("Committed days must not be fetched again")

    report = run_history(
        SOURCES[:1],
        DAYS,
        {"one": dict.fromkeys(DAYS, 12)},
        unexpected_request,
        lambda _: None,
        Event(),
        {"one": {"transfer_events_matched": 62}},
    )
    assert report["status"] == "succeeded"
    assert report["sources"][0]["counts"]["transfer_events_matched"] == 62


def test_failed_source_stops_at_hole_other_rms_continues():
    calls = []

    def sync(source, day):
        calls.append((source, day))
        if source == "one":
            raise SyncError("events_invalid_xml")
        return result(source, day)

    report = run_history(SOURCES, DAYS, {"one": {}, "two": {}}, sync, lambda _: None, Event())
    assert report["status"] == "failed"
    assert calls.count(("one", START)) == 1 and ("one", DAYS[1]) not in calls
    assert report["sources"][0]["counts"]["completed_days"] == 0
    assert report["sources"][1]["status"] == "succeeded"


def test_stop_finishes_current_day_without_claiming_remaining_days():
    stop = Event()

    def sync(source, day):
        stop.set()
        return result(source, day)

    report = run_history(SOURCES, DAYS, {"one": {}, "two": {}}, sync, lambda _: None, stop)
    assert report["status"] == "interrupted"
    assert report["sources"][0]["counts"]["completed_days"] == 1
    assert report["sources"][1]["counts"]["completed_days"] == 0


def test_transient_retry_commits_only_once():
    calls = []

    class NoDelay(Event):
        def wait(self, timeout=None):
            return self.is_set()

    def sync(source, day):
        calls.append(1)
        if len(calls) == 1:
            raise SyncError("events_http_503")
        return result(source, day)

    report = run_history(SOURCES[:1], DAYS[:1], {"one": {}}, sync, lambda _: None, NoDelay())
    assert len(calls) == 2 and report["status"] == "succeeded"
    assert report["sources"][0]["counts"]["events_read"] == 12
    assert report["sources"][0]["error_code"] is None


def test_today_is_explicitly_partial():
    from app.sync_event_history import ZONE

    today = datetime.now(ZONE).date()
    report = run_history(SOURCES[:1], [today], {"one": {}}, result, lambda _: None, Event())
    assert report["sources"][0]["partial_day"] == str(today)


def test_atomic_progress_file_and_event_projection(tmp_path, monkeypatch):
    def checkpoint(report):
        write_json(tmp_path / "events-history-latest.json", report)

    run_history(SOURCES, DAYS, {"one": {}, "two": {}}, result, checkpoint, Event())
    assert (tmp_path / "events-history-latest.json").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(progress, "process_alive", lambda *_: False)
    jobs = progress.progress_snapshot(tmp_path)["jobs"]
    assert len(jobs) == 6
    assert jobs[-1]["resource"] == "events-two"
    assert jobs[-1]["mode"] == "events_history"
    assert jobs[-1]["status"] == "succeeded"
    assert jobs[-1]["documents"] == 36 and jobs[-1]["items"] == 1


def test_dead_queued_event_worker_does_not_look_running(tmp_path, monkeypatch):
    report = {
        "sources": [
            {
                "source_id": "one",
                "label": "One",
                "status": "waiting",
                "counts": {"completed_days": 0, "total_days": 3},
            }
        ]
    }
    (tmp_path / "events-history-latest.json").write_text(json.dumps(report))
    monkeypatch.setattr(progress, "process_alive", lambda *_: False)
    assert progress.progress_snapshot(tmp_path)["jobs"][-1]["status"] == "interrupted"


def test_invalid_range_never_starts_network_requests():
    with pytest.raises(SyncError):
        dates_between(DAYS[2], DAYS[0])
    with pytest.raises(SyncError):
        dates_between(START, date(2200, 1, 1))
