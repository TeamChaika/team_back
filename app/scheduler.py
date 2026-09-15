"""Backend cron worker: durable slots, one leader, sequential iiko requests."""

import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
import psycopg

from app.core.config import BACKEND_DIR, Settings
from app.sync_references import SyncError
from app.web.coverage import ZONE

log = logging.getLogger(__name__)
LEADER_LOCK = 7623011091051
API = "http://127.0.0.1:8010"


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    hour: int | None = None
    minute: int = 0

    def slot(self, now):
        local = now.astimezone(ZONE)
        slot = local.replace(
            hour=self.hour if self.hour is not None else local.hour,
            minute=self.minute,
            second=0,
            microsecond=0,
        )
        if slot > local:
            slot -= timedelta(days=1) if self.hour is not None else timedelta(hours=1)
        return slot

    @property
    def schedule(self):
        return (
            f"{self.hour:02}:{self.minute:02} ежедневно"
            if self.hour is not None
            else f"Каждый час, :{self.minute:02}"
        )


JOBS = (
    Job("sales", "Продажи OLAP · вчера", 6, 0),
    Job("documents", "Накладные, списания и перемещения · вчера и сегодня", minute=10),
    Job("events", "События всех RMS · последние 7 дней", minute=20),
    Job("balances", "Остатки на складах", minute=30),
    Job("document_history", "Документы · открытые 60 дней", 3, 0),
    Job("references", "Структура, сотрудники, должности и справочники", 4, 30),
    Job("inventory", "Номенклатура и техкарты", 5, 0),
    Job("cash_shifts", "Кассовые смены · открытые 60 дней", 6, 30),
    Job("money_balances", "Денежные балансы", 7, 0),
    Job("sales_history", "Продажи OLAP · открытые 60 дней", 2, 0),
)


def run_job(job, slot, settings, stop):
    # All existing loaders take the same database advisory lock and release iiko tokens.
    from app.services.sync_jobs import ROOT
    from app.sync_invoices import HistoryRequest, synchronize_histories

    day = slot.astimezone(ZONE).date()
    yesterday = day - timedelta(days=1)
    root = ROOT / "scheduled" / job.key / slot.strftime("%Y%m%dT%H%M")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if job.key in {"sales", "sales_history"}:
        from app.sync_sales_history import synchronize_history

        # Release the shared iiko lock between weekly captures.
        first = day - timedelta(days=60) if job.key == "sales_history" else yesterday
        while first <= yesterday:
            if stop.is_set():
                raise SyncError("scheduled_sync_interrupted")
            end = min(first + timedelta(days=6), yesterday)
            result = synchronize_history(
                settings,
                first,
                end,
                stop,
                directory=root / first.isoformat(),
                reports=root / "raw",
                refresh=True,
            )
            if result["status"] != "succeeded":
                raise SyncError("scheduled_sales_incomplete")
            first = end + timedelta(days=1)
            stop.wait(1)
    elif job.key in {"documents", "document_history"}:
        start = day - timedelta(days=59) if job.key == "document_history" else yesterday
        # Release the source lock between days so live reports can run too.
        for offset in range((day - start).days + 1):
            if stop.is_set():
                raise SyncError("scheduled_sync_interrupted")
            target = start + timedelta(days=offset)
            synchronize_histories(
                settings,
                API,
                target,
                target,
                [
                    HistoryRequest(resource, root / f"{target}-{resource}.json")
                    for resource in (
                        "incoming_invoices",
                        "writeoffs",
                        "outgoing_invoices",
                        "transfers",
                    )
                ],
                stop,
            )
    elif job.key == "events":
        from app.sync_event_history import synchronize_history

        result = synchronize_history(settings, day - timedelta(days=6), day, stop, directory=root)
        if result["status"] != "succeeded":
            raise SyncError("scheduled_events_incomplete")
    elif job.key in {"balances", "money_balances"}:
        from app.schemas.iiko_reports import AccountingReportQuery
        from app.sync_counteragent_balances import synchronize_counteragent_balances
        from app.sync_store_balances import synchronize_store_balances

        fn = (
            synchronize_store_balances
            if job.key == "balances"
            else synchronize_counteragent_balances
        )
        fn(
            settings,
            AccountingReportQuery(timestamp=datetime.now(ZONE).replace(tzinfo=None, microsecond=0)),
        )
    elif job.key == "inventory":
        from app.sync_inventory import synchronize_inventory

        synchronize_inventory(settings, API, day, root / "inventory.json")
    elif job.key == "cash_shifts":
        from app.schemas.sync_jobs import CashShiftSyncQuery
        from app.sync_cash_shifts import synchronize_cash_shifts

        for offset in range(0, 60, 7):
            if stop.is_set():
                raise SyncError("scheduled_sync_interrupted")
            start = day - timedelta(days=60 - offset)
            end = min(start + timedelta(days=6), yesterday)
            synchronize_cash_shifts(
                settings, CashShiftSyncQuery(open_date_from=start, open_date_to=end), stop=stop
            )
    elif job.key == "references":
        from app.sync_accounts import synchronize_accounts
        from app.sync_dictionaries import synchronize_dictionaries
        from app.sync_employee_roles import synchronize_employee_roles
        from app.sync_employees import synchronize_employees
        from app.sync_references import synchronize

        synchronize(settings, API, root / "references.json")
        for fn in (
            synchronize_employees,
            synchronize_employee_roles,
            synchronize_dictionaries,
            synchronize_accounts,
        ):
            if stop.is_set():
                raise SyncError("scheduled_sync_interrupted")
            fn(settings, API)


def run_due(db, settings, stop, *, now=None, execute=run_job):
    now = now or datetime.now(UTC)
    for job in JOBS:
        if stop.is_set():
            break
        slot = job.slot(now)
        previous = db.execute(
            "SELECT status,next_retry_at FROM chaika.scheduled_sync_runs WHERE job=%s AND slot=%s",
            (job.key, slot),
        ).fetchone()
        if previous and (previous[0] == "succeeded" or (previous[1] and previous[1] > now)):
            continue
        db.execute(
            "INSERT INTO chaika.scheduled_sync_runs(job,slot,status) VALUES(%s,%s,'running') "
            "ON CONFLICT(job,slot) DO UPDATE SET status='running',"
            "started_at=now(),finished_at=NULL,"
            "attempts=chaika.scheduled_sync_runs.attempts+1,error_code=NULL,next_retry_at=NULL",
            (job.key, slot),
        )
        try:
            execute(job, slot, settings, stop)
        except Exception as error:
            from app.sync_sales_history import error_code

            code = error_code(error)
            db.execute(
                "UPDATE chaika.scheduled_sync_runs SET status='failed',"
                "finished_at=now(),error_code=%s,"
                "next_retry_at=now()+interval '5 minutes' WHERE job=%s AND slot=%s",
                (code, job.key, slot),
            )
            log.warning("Scheduled sync failed: %s (%s)", job.key, code)
        else:
            db.execute(
                "UPDATE chaika.scheduled_sync_runs SET status='succeeded',"
                "finished_at=now(),error_code=NULL "
                "WHERE job=%s AND slot=%s",
                (job.key, slot),
            )


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if not settings.sync_enabled:
        return
    if not settings.iiko_configured or not settings.database_url.get_secret_value():
        raise SystemExit("Scheduler requires iiko and database configuration")
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    collector = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
            "--no-access-log",
        ],
        cwd=BACKEND_DIR,
    )
    try:
        for _ in range(30):
            if stop.is_set() or collector.poll() is not None:
                return
            try:
                if httpx.get(API + "/api/v1/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            stop.wait(1)
        else:
            raise SyncError("scheduler_collector_unavailable")
        while not stop.is_set():
            try:
                with psycopg.connect(
                    settings.database_url.get_secret_value(),
                    autocommit=True,
                    connect_timeout=10,
                    application_name="chaika-scheduler",
                ) as db:
                    if not db.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK,)).fetchone()[
                        0
                    ]:
                        stop.wait(15)
                        continue
                    try:
                        # Retry slots left behind by a vanished leader.
                        db.execute(
                            "UPDATE chaika.scheduled_sync_runs SET status='failed',"
                            "error_code='interrupted',"
                            "finished_at=now(),next_retry_at=now() WHERE status='running'"
                        )
                        while not stop.is_set() and collector.poll() is None:
                            run_due(db, settings, stop)
                            stop.wait(15)
                    finally:
                        db.execute("SELECT pg_advisory_unlock(%s)", (LEADER_LOCK,))
            except psycopg.Error as error:
                log.warning("Scheduler database unavailable: %s", type(error).__name__)
                stop.wait(15)
            if collector.poll() is not None:
                raise SyncError("scheduler_collector_stopped")
    finally:
        collector.terminate()
        try:
            collector.wait(timeout=15)
        except subprocess.TimeoutExpired:
            collector.kill()
            collector.wait()


def supervise():
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        worker = subprocess.Popen(
            [sys.executable, "-u", "-m", "app.scheduler", "--worker"],
            cwd=BACKEND_DIR,
            start_new_session=True,
        )
        try:
            while worker.poll() is None and not stop.wait(2):
                pass
        finally:
            stop_process(worker)
        if not stop.is_set():
            log.warning("Restarting scheduled sync worker")
            stop.wait(15)


def start_process():
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "app.scheduler"], cwd=BACKEND_DIR, start_new_session=True
    )


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=25)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        main()
    else:
        supervise()
