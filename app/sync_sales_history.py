"""Resume OLAP history with sequential weekly captures and daily commits."""

import argparse
import asyncio
import fcntl
import json
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg

from app.core.config import BACKEND_DIR, Settings
from app.import_sales_review import KINDS, parse_review, publish
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.services.iiko_auth import IikoAuthService
from app.services.sync_jobs import write_json
from app.sync_references import SyncError, configured_sources, reference_lock, register_sources

DIRECTORY = BACKEND_DIR / ".local/sync"
REPORTS = BACKEND_DIR / ".local/reports"
ZONE = ZoneInfo("Europe/Simferopol")
REPORT_ORDER = ("daily", "dishes", "payments", "discounts", "returns", "waiters", "hours")


def error_code(error: Exception) -> str:
    if isinstance(error, IikoError):
        return error.code
    if isinstance(error, SyncError):
        return str(error)
    if isinstance(error, ValueError) and str(error).startswith("sales_"):
        return str(error)
    return type(error).__name__


def history_days(start: date, end: date, *, include_today=False) -> list[date]:
    today = datetime.now(ZONE).date()
    if (
        start < date(2000, 1, 1)
        or end < start
        or end > today
        or (end == today and not include_today)
    ):
        raise SyncError("invalid_sales_history_period")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def approved_templates(db):
    row = db.execute(
        "SELECT id FROM chaika.sales_report_sets WHERE source_id='primary' AND reviewed "
        "ORDER BY business_date DESC,observed_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise SyncError("sales_approved_templates_missing")
    templates = dict(
        db.execute("SELECT kind,request FROM chaika.sales_reports WHERE set_id=%s", row).fetchall()
    )
    if set(templates) != KINDS:
        raise SyncError("sales_approved_templates_incomplete")
    for request in templates.values():
        if (
            request["reportType"] != "SALES"
            or request["buildSummary"] is not False
            or request.get("groupByColFields")
            or len(request["groupByRowFields"]) + len(request["aggregateFields"]) > 7
            or request["filters"] != templates["daily"]["filters"]
        ):
            raise SyncError("sales_approved_templates_invalid")
    if "HourOpen" not in templates["hours"]["groupByRowFields"]:
        raise SyncError("sales_wrong_hour")
    if "PayTypes.Group" not in templates["payments"]["groupByRowFields"]:
        raise SyncError("sales_wrong_payment_group")
    return str(row[0]), templates


@dataclass(frozen=True)
class FixedReport:
    body: dict

    def iiko_body(self):
        return self.body


def request_for_day(template: dict, day: date, end: date | None = None) -> FixedReport:
    end = end or day
    if end < day or (end - day).days > 6:
        raise SyncError("sales_invalid_capture_period")
    body = deepcopy(template)
    body["filters"]["OpenDate.Typed"] = {
        "filterType": "DateRange",
        "from": datetime.combine(day, time.min).isoformat(),
        "to": datetime.combine(end + timedelta(days=1), time.min).isoformat(),
        "includeLow": True,
        "includeHigh": False,
    }
    return FixedReport(body)


async def collect_day(
    settings,
    source,
    day,
    templates,
    template_id,
    root: Path,
    stop: Event,
    *,
    end: date | None = None,
):
    """Keep RAW on failure; publish only complete captures with confirmed logout."""
    (root / "raw").mkdir(parents=True, mode=0o700)
    manifest = dict(
        business_date=day.isoformat(),
        source_fingerprint=source.fingerprint,
        template_set_id=template_id,
        reports={},
        errors={},
        started_at=datetime.now(UTC).isoformat(),
    )
    if end is not None:
        manifest.update(date_to=end.isoformat(), capture_id=str(uuid4()))
    client = IikoClient(settings)
    auth = IikoAuthService(settings, client)
    try:
        for kind in REPORT_ORDER:
            if stop.is_set():
                raise SyncError("sales_history_interrupted")
            query = request_for_day(templates[kind], day, end)
            path = root / "raw" / f"{kind}.json"
            result = await auth.download_daily_sales(path, query=query)
            payload = json.loads(path.read_bytes(), parse_float=Decimal)
            manifest["reports"][kind] = dict(
                raw_file=f"raw/{kind}.json",
                request=query.iiko_body(),
                rows=len(payload["data"]),
                sha256=result.sha256,
                source_bytes=result.size_bytes,
                received_at=datetime.now(UTC).isoformat(),
            )
            write_json(root / "manifest.json", manifest)
    except Exception as error:
        manifest["errors"]["collection"] = error_code(error)
        raise
    finally:
        try:
            manifest["logout"] = (await auth.logout()).state
            if manifest["logout"] != "logged_out":
                raise SyncError("sales_logout_failed")
        except Exception as error:
            manifest["errors"]["logout"] = error_code(error)
            raise SyncError("sales_logout_failed") from None
        finally:
            try:
                await client.aclose()
            finally:
                manifest["finished_at"] = datetime.now(UTC).isoformat()
                write_json(root / "manifest.json", manifest)


def run_history(
    days, coverage, sync_day, checkpoint, stop: Event, *, template_id=None, warning_days=()
):
    coverage = {day: dict(counts) for day, counts in coverage.items() if day in days}
    resumed = len(coverage)
    warnings = set(warning_days)
    report = dict(
        run_id=str(uuid4()), status="running", error_code=None, template_set_id=template_id
    )

    def save():
        remaining = [day for day in days if day not in coverage]
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
            documents_read=sum(len(value) for value in coverage.values()),
            items_read=sum(sum(value.values()) for value in coverage.values()),
            completed_through=through,
            next_date=remaining[0].isoformat() if remaining else None,
            warning_days=len(warnings),
        )
        checkpoint(report)

    save()
    try:
        for day in days:
            if day in coverage:
                continue
            if stop.is_set():
                raise SyncError("sales_history_interrupted")
            # sync_day returns only after parse_review, confirmed logout and DB commit.
            result = sync_day(day)
            if result.get("status") != "imported" or set(result.get("rows", {})) != KINDS:
                raise SyncError("sales_history_day_not_committed")
            coverage[day] = result["rows"]
            if result.get("warning_count", 0):
                warnings.add(day)
            save()
        report["status"] = "succeeded"
    except Exception as error:
        code = error_code(error)
        report.update(
            status="interrupted" if code == "sales_history_interrupted" else "failed",
            error_code=code,
        )
    save()
    return report


def missing_windows(days, coverage):
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


def run_prefetched_history(days, coverage, collect_window, publish_day, checkpoint, stop, **kwargs):
    """One network worker overlaps the next capture with day-by-day DB publication."""
    windows = iter(missing_windows(days, coverage))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sales-capture") as executor:
        current = None
        window = next(windows, None)
        pending = executor.submit(collect_window, *window) if window else None

        def sync_day(day):
            nonlocal current, window, pending
            if current is None or day > current[1]:
                root = pending.result()
                current = (*window, root)
                window = next(windows, None)
                pending = executor.submit(collect_window, *window) if window else None
            return publish_day(current[2], day)

        try:
            report = run_history(days, coverage, sync_day, checkpoint, stop, **kwargs)
        finally:
            stop.set()
        # Drain the in-flight capture so logout finishes before the overall run exits.
        if pending:
            try:
                pending.result()
            except Exception as error:
                code = error_code(error)
                if code != "sales_history_interrupted":
                    report["prefetch_error"] = code
                    if code == "sales_logout_failed":
                        report.update(status="failed", error_code=code)
                    checkpoint(report)
        return report


def synchronize_history(
    settings,
    start,
    end,
    stop,
    directory=DIRECTORY,
    reports=REPORTS,
    *,
    include_today=False,
    refresh=False,
):
    days = history_days(start, end, include_today=include_today)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "sales-history.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("sales_history_already_running") from None
        source = configured_sources(settings)[0]
        with (
            psycopg.connect(
                settings.database_url.get_secret_value(), autocommit=True, connect_timeout=10
            ) as db,
            reference_lock(db),
        ):
            register_sources(db, [source])
            template_id, templates = approved_templates(db)
            rows = db.execute(
                "SELECT d.business_date,jsonb_object_agg(r.kind,r.row_count) "
                "FROM chaika.sales_report_days d "
                "JOIN chaika.sales_report_sets s ON s.id=d.current_set_id "
                "JOIN chaika.sales_reports r ON r.set_id=s.id "
                "WHERE d.source_id=%s AND d.business_date BETWEEN %s AND %s "
                "GROUP BY d.business_date HAVING count(*)=7 "
                "AND min(r.observed_at) >= ((d.business_date + 1)::timestamp "
                "AT TIME ZONE 'Europe/Simferopol')",
                (source.id, start, end),
            ).fetchall()
            coverage = {day: counts for day, counts in rows if set(counts) == KINDS}
            if refresh:
                coverage = {}
            warning_days = {
                row[0]
                for row in db.execute(
                    "SELECT d.business_date FROM chaika.sales_report_days d JOIN "
                    "chaika.sales_report_sets s ON s.id=d.current_set_id "
                    "WHERE d.source_id=%s AND d.business_date BETWEEN %s AND %s "
                    "AND EXISTS(SELECT 1 FROM jsonb_array_elements(s.checks) c "
                    "WHERE c->>'exact_match'='false')",
                    (source.id, start, end),
                ).fetchall()
            }
            # Release any token cached by the technical API before using this worker's session.
            with httpx.Client(timeout=30, trust_env=False) as http:
                response = http.post("http://127.0.0.1:8010/api/v1/iiko/connections/primary/logout")
                if response.status_code != 200 or response.json().get("state") != "logged_out":
                    raise SyncError("sales_api_logout_failed")
            write_json(
                directory / "sales-process.json",
                dict(pid=os.getpid(), date_from=start.isoformat(), date_to=end.isoformat()),
            )

            def checkpoint(report):
                if (
                    end == datetime.now(ZONE).date()
                    and report["counts"]["completed_through"] == end.isoformat()
                ):
                    report["partial_day"] = end.isoformat()
                    report["partial_as_of"] = report["updated_at"]
                write_json(directory / f"sales-{report['run_id']}.json", report)
                write_json(directory / "sales-latest.json", report)
                print(json.dumps(report, ensure_ascii=False), flush=True)

            def collect_window(first, last):
                root = reports / f"sales-{first.isoformat()}-{last.isoformat()}-{uuid4()}"
                asyncio.run(
                    collect_day(
                        settings, source, first, templates, template_id, root, stop, end=last
                    )
                )
                return root

            def publish_day(root, day):
                bundle = parse_review(
                    root, source.fingerprint, business_date=day, allow_discrepancies=True
                )
                return publish(db, bundle)

            return run_prefetched_history(
                days,
                coverage,
                collect_window,
                publish_day,
                checkpoint,
                stop,
                template_id=template_id,
                warning_days=warning_days,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument("--date-to", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--include-today", action="store_true", help="Include a preliminary current-day snapshot"
    )
    args = parser.parse_args()
    os.umask(0o077)
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        result = synchronize_history(
            Settings(), args.date_from, args.date_to, stop, include_today=args.include_today
        )
    except Exception as error:
        print(json.dumps(dict(status="failed", error_code=error_code(error))), flush=True)
        raise SystemExit(1) from None
    if result["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
