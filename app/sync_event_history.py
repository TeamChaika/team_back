"""Resume RMS event history from committed source/day coverage, one request at a time."""

import argparse
import fcntl
import json
import os
import signal
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg

from app.core.config import BACKEND_DIR, Settings
from app.schemas.iiko_events import EventsSyncQuery
from app.sync_events import synchronize_events
from app.sync_references import (
    Source,
    SyncError,
    configured_sources,
    reference_lock,
    register_sources,
)

ZONE = ZoneInfo("Europe/Simferopol")
DIRECTORY = BACKEND_DIR / ".local/sync"
RETRYABLE = {
    "sync_already_running",
    "OperationalError",
    "events_http_429",
    "events_http_502",
    "events_http_503",
    "events_http_504",
}


def write_json(path: Path, payload: dict):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def dates_between(start: date, end: date) -> list[date]:
    today = datetime.now(ZONE).date()
    if start < date(2000, 1, 1) or end < start or end > today:
        raise SyncError("invalid_events_history_period")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def completed_coverage(rows: list[tuple], days: list[date]) -> dict[date, int]:
    """An export collected before the day's end must be refreshed on resume."""
    scope = set(days)
    return {
        day: count
        for day, count, observed in rows
        if day in scope and observed >= datetime.combine(day + timedelta(days=1), time.min, ZONE)
    }


def run_history(
    sources: list[Source],
    days: list[date],
    coverage: dict[str, dict[date, int]],
    sync_day,
    checkpoint,
    stop: Event,
    link_counts: dict[str, dict[str, int]] | None = None,
) -> dict:
    report = {
        "run_id": str(uuid4()),
        "status": "running",
        "sources": [],
        "date_from": days[0].isoformat(),
        "date_to": days[-1].isoformat(),
    }
    for source in sources:
        report["sources"].append(
            {
                "source_id": source.id,
                "label": source.label,
                "status": "waiting",
                "run_id": report["run_id"],
                "error_code": None,
                "counts": {
                    **(link_counts or {}).get(source.id, {}),
                    "date_from": report["date_from"],
                    "date_to": report["date_to"],
                    "total_days": len(days),
                    "resumed_days": len(coverage[source.id]),
                },
            }
        )

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        for item in report["sources"]:
            committed = coverage[item["source_id"]]
            remaining = [day for day in days if day not in committed]
            through = None
            for day in days:
                if day not in committed:
                    break
                through = day.isoformat()
            item["updated_at"] = report["updated_at"]
            item["counts"].update(
                {
                    "completed_days": len(committed),
                    "events_read": sum(committed.values()),
                    "completed_through": through,
                    "next_date": remaining[0].isoformat() if remaining else None,
                }
            )
            if not remaining and item["status"] != "failed":
                item["status"] = "succeeded"
        checkpoint(report)

    save()
    for day in days:
        for item in report["sources"]:
            source_id = item["source_id"]
            if stop.is_set():
                break
            if day in coverage[source_id] or item["status"] == "failed":
                continue
            item["status"] = "running"
            save()
            for attempt in range(3):
                try:
                    result = sync_day(source_id, day)
                    if result["status"] != "succeeded":
                        raise SyncError("events_day_not_committed")
                    coverage[source_id][day] = result["counts"]["events"]
                    item["last_day_run_id"] = result["run_id"]
                    for state in ("matched", "pending", "ambiguous", "invalid"):
                        key = f"transfer_events_{state}"
                        item["counts"][key] = result["counts"].get(key, 0)
                    if day == datetime.now(ZONE).date():
                        item["partial_day"] = day.isoformat()
                        item["partial_as_of"] = datetime.now(UTC).isoformat()
                    item["error_code"] = None
                    item["status"] = "waiting"
                    break
                except Exception as error:
                    code = str(error) if isinstance(error, SyncError) else type(error).__name__
                    item["error_code"] = code
                    save()
                    if code in RETRYABLE and attempt < 2 and not stop.wait(5 * (attempt + 1)):
                        continue
                    item["status"] = "interrupted" if stop.is_set() else "failed"
                    break
            save()
        if stop.is_set():
            break
    for item in report["sources"]:
        if item["status"] not in {"succeeded", "failed"}:
            item["status"] = "interrupted"
    report["status"] = (
        "succeeded"
        if all(s["status"] == "succeeded" for s in report["sources"])
        else "interrupted"
        if stop.is_set()
        else "failed"
    )
    save()
    return report


def synchronize_history(
    settings: Settings,
    start: date,
    end: date,
    stop: Event,
    directory: Path = DIRECTORY,
):
    days = dates_between(start, end)
    sources = [source for source in configured_sources(settings) if source.id != "primary"]
    if not sources:
        raise SyncError("events_rms_required")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / "events-history.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("events_history_already_running") from None
        with (
            psycopg.connect(
                settings.database_url.get_secret_value(),
                autocommit=True,
                connect_timeout=10,
                application_name="chaika-events-history-preflight",
            ) as db,
            reference_lock(db),
        ):
            # Also verify sources before skipping any previously committed dates.
            with db.transaction():
                register_sources(db, sources)
                matched = {
                    row[0]
                    for row in db.execute(
                        "SELECT s.id FROM chaika.sources s JOIN chaika.rms_bindings b "
                        "ON b.source_id=s.id WHERE s.server_type='REPLICATED_RMS' "
                        "AND b.state='matched'"
                    ).fetchall()
                }
                if any(source.id not in matched for source in sources):
                    raise SyncError("events_rms_mapping_required")
                coverage = {
                    source.id: completed_coverage(
                        db.execute(
                            "SELECT event_date,event_count,observed_at FROM chaika.rms_event_days "
                            "WHERE source_id=%s AND event_date BETWEEN %s AND %s",
                            (source.id, start, end),
                        ).fetchall(),
                        days,
                    )
                    for source in sources
                }
                link_counts = {
                    source.id: {
                        f"transfer_events_{state}": count
                        for state, count in db.execute(
                            "SELECT status,count(*) FROM chaika.rms_event_links "
                            "WHERE source_id=%s GROUP BY status",
                            (source.id,),
                        ).fetchall()
                    }
                    for source in sources
                }
        write_json(
            directory / "events-history-process.json",
            {
                "pid": os.getpid(),
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
            },
        )

        def checkpoint(report):
            write_json(directory / "events-history-latest.json", report)

        return run_history(
            sources,
            days,
            coverage,
            lambda source_id, day: synchronize_events(
                settings, EventsSyncQuery(source_id=source_id, date=day)
            ),
            checkpoint,
            stop,
            link_counts,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, required=True)
    parser.add_argument("--date-to", type=date.fromisoformat, default=datetime.now(ZONE).date())
    args = parser.parse_args()
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        result = synchronize_history(Settings(), args.date_from, args.date_to, stop)
    except Exception as error:
        code = str(error) if isinstance(error, SyncError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
