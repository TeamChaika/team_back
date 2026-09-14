from copy import deepcopy
from datetime import date, timedelta
from threading import Event

import pytest

from app.sync_cash_shift_history import history_days, missing_windows, run_history
from app.sync_references import SyncError


def days(count=10):
    return [date(2026, 8, 1) + timedelta(days=i) for i in range(count)]


def saved(day, shifts=12):
    return dict(day=day.isoformat(), shifts=shifts, matched=shifts - 1, unmapped=1)


def test_windows_skip_committed_days_and_never_exceed_seven_days():
    all_days = days(18)
    assert missing_windows(all_days, {all_days[3]: {}, all_days[15]: {}}) == [
        (all_days[0], all_days[2]),
        (all_days[4], all_days[10]),
        (all_days[11], all_days[14]),
        (all_days[16], all_days[17]),
    ]


@pytest.mark.parametrize(
    "start,end",
    [
        (date(1999, 1, 1), date(2026, 1, 1)),
        (date(2026, 8, 2), date(2026, 8, 1)),
        (date(2026, 8, 1), date(9999, 1, 1)),
    ],
)
def test_invalid_history_scope_is_rejected(start, end):
    with pytest.raises(SyncError):
        history_days(start, end)


def test_resume_counts_previously_committed_days_without_loading_them():
    all_days = days(10)
    calls, checkpoints = [], []

    def sync(query, on_day_saved, stop):
        calls.append(query)
        for day in all_days:
            if query.open_date_from <= day <= query.open_date_to:
                on_day_saved(saved(day))
        return {"counts": {"logout_ok": True}}

    report = run_history(
        all_days,
        {all_days[9]: saved(all_days[9])},
        sync,
        lambda r: checkpoints.append(deepcopy(r)),
        Event(),
    )
    assert report["status"] == "succeeded"
    assert report["counts"]["completed_days"] == 10
    assert report["counts"]["resumed_days"] == 1
    assert report["counts"]["documents_read"] == 120
    assert report["counts"]["items_read"] == 110
    assert calls[-1].open_date_to == all_days[8]
    assert checkpoints[0]["counts"]["completed_through"] is None
    assert report["counts"]["next_date"] is None


@pytest.mark.parametrize("failure", [SyncError("cash_shifts_http_502"), ValueError("private")])
def test_failure_keeps_only_committed_day_and_does_not_leak_details(failure):
    def sync(query, on_day_saved, stop):
        on_day_saved(saved(query.open_date_from))
        raise failure

    report = run_history(days(), {}, sync, lambda r: None, Event())
    assert report["status"] == "failed"
    assert report["counts"]["completed_days"] == 1
    assert report["counts"]["next_date"] == "2026-08-02"
    assert "private" not in str(report)


def test_stop_preserves_checkpoint_and_still_requires_logout():
    stop = Event()

    def sync(query, on_day_saved, stop):
        on_day_saved(saved(query.open_date_from))
        stop.set()
        raise SyncError("cash_shifts_interrupted")

    report = run_history(days(), {}, sync, lambda r: None, stop)
    assert report["status"] == "interrupted" and report["counts"]["completed_days"] == 1


def test_logout_failure_cannot_be_reported_as_success():
    def sync(query, on_day_saved, stop):
        on_day_saved(saved(query.open_date_from))
        return {"counts": {"logout_ok": False}}

    report = run_history(days(1), {}, sync, lambda r: None, Event())
    assert report["status"] == "failed" and report["error_code"] == "cash_shifts_logout_failed"


def test_missing_callback_cannot_fake_completed_window():
    report = run_history(
        days(), {}, lambda *a, **k: {"counts": {"logout_ok": True}}, lambda r: None, Event()
    )
    assert report["status"] == "failed" and report["counts"]["completed_days"] == 0


def test_today_is_explicit_and_future_is_never_allowed():
    from app.sync_cash_shift_history import ZONE, datetime

    today = datetime.now(ZONE).date()
    with pytest.raises(SyncError, match="invalid_cash_shift_history_period"):
        history_days(today, today)
    assert history_days(today, today, include_today=True) == [today]
    with pytest.raises(SyncError, match="invalid_cash_shift_history_period"):
        history_days(today, today + timedelta(days=1), include_today=True)
