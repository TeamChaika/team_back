"""Resumable cash-shift history, sequential opening days and a durable progress screen."""

import argparse
import fcntl
import json
import os
import signal
from datetime import UTC, date, datetime, time, timedelta
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg

from app.core.config import BACKEND_DIR, Settings
from app.schemas.sync_jobs import CashShiftSyncQuery
from app.services.sync_jobs import write_json
from app.sync_cash_shifts import synchronize_cash_shifts
from app.sync_references import SyncError, configured_sources, reference_lock, register_sources

DIRECTORY = BACKEND_DIR / ".local/sync"
ZONE = ZoneInfo("Europe/Simferopol")


def history_days(start: date, end: date, *, include_today=False) -> list[date]:
    today = datetime.now(ZONE).date()
    if (
        start < date(2000, 1, 1)
        or end < start
        or end > today
        or (end == today and not include_today)
    ):
        raise SyncError("invalid_cash_shift_history_period")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def missing_windows(days: list[date], coverage: dict) -> list[tuple[date, date]]:
    windows = []
    for day in days:
        if day in coverage:
            continue
        if (
            windows
            and day == windows[-1][1] + timedelta(days=1)
            and (day - windows[-1][0]).days < 7
        ):
            windows[-1] = (windows[-1][0], day)
        else:
            windows.append((day, day))
    return windows


def run_history(days, coverage, sync_window, checkpoint, stop: Event):
    """Only callbacks after a committed day can advance the visible progress."""
    coverage = {d: dict(value) for d, value in coverage.items() if d in days}
    report = dict(run_id=str(uuid4()), status="running", error_code=None, counts={})
    resumed = len(coverage)

    def save():
        remaining = [d for d in days if d not in coverage]
        through = None
        for day in days:
            if day not in coverage:
                break
            through = day.isoformat()
        report["updated_at"] = datetime.now(UTC).isoformat()
        report["counts"] = dict(
            date_from=days[0].isoformat(),
            date_to=days[-1].isoformat(),
            completed_days=len(coverage),
            total_days=len(days),
            resumed_days=resumed,
            documents_read=sum(v["shifts"] for v in coverage.values()),
            items_read=sum(v["matched"] for v in coverage.values()),
            unmapped=sum(v["unmapped"] for v in coverage.values()),
            completed_through=through,
            next_date=remaining[0].isoformat() if remaining else None,
        )
        checkpoint(report)

    def saved(value):
        day = date.fromisoformat(value["day"])
        if day not in days:
            raise SyncError("cash_shift_history_scope_mismatch")
        coverage[day] = value
        save()

    save()
    try:
        for start, end in missing_windows(days, coverage):
            if stop.is_set():
                raise SyncError("cash_shifts_interrupted")
            result = sync_window(
                CashShiftSyncQuery(open_date_from=start, open_date_to=end),
                on_day_saved=saved,
                stop=stop,
            )
            if not result["counts"]["logout_ok"]:
                raise SyncError("cash_shifts_logout_failed")
        if len(coverage) != len(days):
            raise SyncError("cash_shift_history_incomplete")
        report["status"] = "succeeded"
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        report.update(
            status="interrupted" if code == "cash_shifts_interrupted" else "failed", error_code=code
        )
    save()
    return report


def synchronize_history(settings, start, end, stop, directory=DIRECTORY, *, include_today=False):
    days = history_days(start, end, include_today=include_today)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "cash-shifts-history.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("cash_shift_history_already_running") from None
        source = configured_sources(settings)[0]
        with (
            psycopg.connect(
                settings.database_url.get_secret_value(), autocommit=True, connect_timeout=10
            ) as db,
            reference_lock(db),
        ):
            register_sources(db, [source])
            rows = db.execute(
                "SELECT d.open_day,d.row_count,d.observed_at,"
                "(SELECT count(*) FROM chaika.cash_shift_observations o "
                "WHERE o.snapshot_id=d.last_snapshot_id AND o.mapping_state='matched') "
                "FROM chaika.cash_shift_days d "
                "JOIN chaika.raw_snapshots s ON s.id=d.last_snapshot_id "
                "JOIN chaika.sync_runs r ON r.id=s.run_id "
                "WHERE d.source_id=%s AND d.open_day BETWEEN %s AND %s "
                "AND (r.status='succeeded' OR "
                "(r.status='failed' AND r.counts->>'logout_ok'='true'))",
                (source.id, start, end),
            ).fetchall()
        coverage = {
            day: dict(shifts=count, matched=matched, unmapped=count - matched)
            for day, count, observed, matched in rows
            if observed >= datetime.combine(day + timedelta(days=1), time.min, ZONE)
        }
        write_json(
            directory / "cash-shifts-process.json",
            dict(
                pid=os.getpid(),
                date_from=start.isoformat(),
                date_to=end.isoformat(),
                mode="cash_shift_history",
            ),
        )

        def checkpoint(report):
            if (
                end == datetime.now(ZONE).date()
                and report["counts"]["completed_through"] == end.isoformat()
            ):
                report["partial_day"] = end.isoformat()
                report["partial_as_of"] = report["updated_at"]
            write_json(directory / f"cash-shifts-{report['run_id']}.json", report)
            write_json(directory / "cash-shifts-latest.json", report)
            print(json.dumps(report, ensure_ascii=False), flush=True)

        return run_history(
            days,
            coverage,
            lambda query, **kwargs: synchronize_cash_shifts(settings, query, **kwargs),
            checkpoint,
            stop,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument("--date-to", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--include-today", action="store_true", help="Include a preliminary current-day snapshot"
    )
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        result = synchronize_history(
            Settings(), args.date_from, args.date_to, stop, include_today=args.include_today
        )
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps(dict(status="failed", error_code=code)), flush=True)
        raise SystemExit(1) from None
    if result["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
