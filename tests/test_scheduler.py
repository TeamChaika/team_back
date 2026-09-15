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


def test_sales_history_continues_after_each_prefetch_cleanup(monkeypatch, tmp_path):
    from app import sync_sales_history as history
    from app.import_sales_review import KINDS
    from app.services import sync_jobs

    stop = Event()
    monkeypatch.setattr(stop, "wait", lambda _: False)
    monkeypatch.setattr(sync_jobs, "ROOT", tmp_path)
    windows, published = [], []

    def capture(settings, first, last, capture_stop, **kwargs):
        windows.append((first, last))

        def publish(root, day):
            published.append(day)
            return {"status": "imported", "rows": dict.fromkeys(KINDS, 1)}

        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        return history.run_prefetched_history(
            days, {}, lambda *_: tmp_path, publish, lambda _: None, capture_stop
        )

    monkeypatch.setattr(history, "synchronize_history", capture)
    slot = datetime(2026, 9, 15, 2, tzinfo=scheduler.ZONE)
    scheduler.run_job(scheduler.Job("sales_history", "History", 2), slot, None, stop)
    assert len(windows) == 9
    assert len(set(published)) == 60
    assert published[0] == slot.date() - timedelta(days=60)
    assert published[-1] == slot.date() - timedelta(days=1)
    assert not stop.is_set()


def test_capture_stop_observes_shutdown_without_stopping_other_captures():
    stop = Event()
    first, second = scheduler.CaptureStop(stop), scheduler.CaptureStop(stop)
    first.set()
    assert first.is_set() and not second.is_set() and not stop.is_set()
    stop.set()
    assert second.is_set()


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
