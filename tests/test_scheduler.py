from datetime import UTC, datetime, timedelta
from threading import Event

from test_reference_sync import db as db

from app import scheduler


def test_daily_and_hourly_slots_use_local_calendar():
    daily = scheduler.Job("daily", "Test", 6)
    hourly = scheduler.Job("hourly", "Test", minute=10)
    now = datetime(2026, 9, 15, 2, 59, tzinfo=UTC)
    assert daily.slot(now).isoformat() == "2026-09-14T06:00:00+03:00"
    assert daily.slot(now + timedelta(minutes=1)).day == 15
    assert hourly.slot(now).isoformat() == "2026-09-15T05:10:00+03:00"


def test_completed_slot_not_repeated_after_restart_and_failed_slot_retried(db, monkeypatch):
    job = scheduler.Job("test", "Test", 6)
    monkeypatch.setattr(scheduler, "JOBS", (job,))
    calls = []
    now = datetime.now(UTC)

    def fail(*_):
        calls.append(1)
        raise ValueError("private credentials")

    scheduler.run_due(db, None, Event(), now=now, execute=fail)
    row = db.execute("SELECT status,attempts,error_code FROM chaika.scheduled_sync_runs").fetchone()
    assert row[0] == "failed" and row[1] == 1 and "private" not in row[2]
    scheduler.run_due(db, None, Event(), now=now, execute=fail)
    assert len(calls) == 1
    db.execute("UPDATE chaika.scheduled_sync_runs SET next_retry_at=now()-interval '1 hour'")
    scheduler.run_due(db, None, Event(), now=now, execute=lambda *_: calls.append(2))
    scheduler.run_due(db, None, Event(), now=now, execute=fail)
    assert calls == [1, 2]
    assert db.execute("SELECT status,attempts FROM chaika.scheduled_sync_runs").fetchone() == (
        "succeeded",
        2,
    )
