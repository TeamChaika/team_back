"""Durable manual requests executed only by the existing scheduler leader."""

import logging
import math
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from app.tenancy.connection import tenant_connect
from app.tenancy.io import namespaced_lock
from app.tenancy.sql import ANALYTICS_SCHEMA

COOLDOWN = timedelta(minutes=10)
log = logging.getLogger(__name__)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


def job_lock(db, job):
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (namespaced_lock("manual-sync:" + job),),
    )


def available(db):
    with db.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            "SELECT available AND heartbeat_at>clock_timestamp()-interval '45 seconds' AS ready "
            f"FROM {ANALYTICS_SCHEMA}.scheduler_runtime WHERE singleton"
        ).fetchone()
        return bool(row and row["ready"])


def latest(db):
    with db.cursor(row_factory=dict_row) as cursor:
        return {
            row["job"]: row
            for row in cursor.execute(
                f"SELECT DISTINCT ON(job) * FROM {ANALYTICS_SCHEMA}.manual_sync_requests "
                "ORDER BY job,requested_at DESC"
            ).fetchall()
        }


def manual_state(row, now, *, ready, authorized, automatic_running=False):
    next_run = row["requested_at"] + COOLDOWN if row else None
    remaining = max(0, math.ceil((next_run - now).total_seconds())) if next_run else 0
    state = row["state"] if row else None
    blocked = (
        "permission"
        if not authorized
        else "scheduler_unavailable"
        if not ready
        else "running"
        if automatic_running or state == "running"
        else "pending"
        if state == "pending"
        else "cooldown"
        if remaining
        else None
    )
    return {
        "can_run": blocked is None,
        "blocked_reason": blocked,
        "next_manual_run_at": next_run,
        "remaining_seconds": remaining,
        **{
            key: row[key] if row else None
            for key in (
                "state",
                "request_id",
                "requested_at",
                "started_at",
                "finished_at",
                "error_code",
            )
        },
    }


def request_run(db, job, request_id, actor_id, *, enabled):
    """Caller owns a transaction; a shared job lock serializes manual and cron reservations."""
    from app.scheduler import JOBS

    if job not in {item.key for item in JOBS}:
        raise HTTPException(404, "Задача синхронизации не найдена.")
    with db.cursor(row_factory=dict_row) as cursor:
        # A request UUID belongs to one actor/job, even across concurrent job requests.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (namespaced_lock(str(request_id)),),
        )
        job_lock(db, job)
        previous = cursor.execute(
            f"SELECT * FROM {ANALYTICS_SCHEMA}.manual_sync_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()
        if previous:
            if previous["job"] != job or str(previous["requested_by"]) != str(actor_id):
                raise HTTPException(409, "Этот идентификатор уже использован другим запросом.")
            return result(previous)
        if not enabled or not available(db):
            raise HTTPException(503, "Планировщик недоступен. Запуск не принят; повторите позже.")
        automatic = cursor.execute(
            (
                f"SELECT 1 FROM {ANALYTICS_SCHEMA}.scheduled_sync_runs WHERE job=%s AND "
                f"status='running' LIMIT 1"
            ),
            (job,),
        ).fetchone()
        row = cursor.execute(
            f"SELECT * FROM {ANALYTICS_SCHEMA}.manual_sync_requests WHERE job=%s "
            "ORDER BY requested_at DESC LIMIT 1",
            (job,),
        ).fetchone()
        now = cursor.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        state = manual_state(
            row, now, ready=True, authorized=True, automatic_running=bool(automatic)
        )
        if state["blocked_reason"] in {"pending", "running"}:
            raise HTTPException(409, "Эта задача уже выполняется или ожидает запуска.")
        if state["remaining_seconds"]:
            raise HTTPException(
                429,
                "Ручной запуск каждой задачи доступен раз в 10 минут.",
                headers={"Retry-After": str(state["remaining_seconds"])},
            )
        row = cursor.execute(
            f"INSERT INTO {ANALYTICS_SCHEMA}.manual_sync_requests(request_id,job,requested_by) "
            "VALUES(%s,%s,%s) RETURNING *",
            (request_id, job, actor_id),
        ).fetchone()
        return result(row)


def result(row):
    return {
        **{key: row[key] for key in ("request_id", "job", "state", "requested_at")},
        "next_manual_run_at": row["requested_at"] + COOLDOWN,
    }


def run_one(db, settings, stop, *, execute):
    """Only called under the scheduler leader lock; no automatic retries of manual requests."""
    from app.scheduler import JOBS
    from app.sync_sales_history import error_code

    if stop.is_set():
        return False
    with db.transaction(), db.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            f"SELECT * FROM {ANALYTICS_SCHEMA}.manual_sync_requests WHERE state='pending' "
            "ORDER BY requested_at FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not row:
            return False
        job_lock(db, row["job"])
        # A running automatic slot is never overlapped, even if this function is called alone.
        if cursor.execute(
            (
                f"SELECT 1 FROM {ANALYTICS_SCHEMA}.scheduled_sync_runs WHERE job=%s AND "
                f"status='running' LIMIT 1"
            ),
            (row["job"],),
        ).fetchone():
            return False
        started_at = cursor.execute(
            f"UPDATE {ANALYTICS_SCHEMA}.manual_sync_requests SET state='running',"
            f"started_at=clock_timestamp() "
            "WHERE request_id=%s RETURNING started_at",
            (row["request_id"],),
        ).fetchone()["started_at"]
    job = next((job for job in JOBS if job.key == row["job"]), None)
    try:
        if job is None:
            raise ValueError("Unknown manual job")
        execute(job, started_at, settings, stop)
    except Exception as error:
        state, code = "failed", error_code(error)
        log.warning("Manual sync failed: %s (%s)", row["job"], code)
    else:
        state, code = "succeeded", None
    db.execute(
        f"UPDATE {ANALYTICS_SCHEMA}.manual_sync_requests SET state=%s,"
        "finished_at=clock_timestamp(),error_code=%s "
        "WHERE request_id=%s AND state='running'",
        (state, code, row["request_id"]),
    )
    return True


@contextmanager
def heartbeat(settings, leader, collector):
    """Independent small DB connection keeps long-running sync jobs visibly alive."""
    instance = uuid4()
    leader.execute(
        f"INSERT INTO {ANALYTICS_SCHEMA}.scheduler_runtime(singleton,instance_id) VALUES(true,%s) "
        "ON CONFLICT(singleton) DO UPDATE SET instance_id=excluded.instance_id,"
        "available=true,heartbeat_at=clock_timestamp()",
        (instance,),
    )
    leader.execute(
        f"UPDATE {ANALYTICS_SCHEMA}.manual_sync_requests SET state='failed',"
        f"finished_at=clock_timestamp(),"
        "error_code='interrupted' WHERE state='running'"
    )
    stopped = Event()

    def pulse():
        while not stopped.wait(10):
            try:
                with tenant_connect(
                    settings.database_url.get_secret_value(),
                    autocommit=True,
                    connect_timeout=5,
                    application_name="chaika-scheduler-heartbeat",
                    connector=psycopg.connect,
                ) as connection:
                    connection.execute("SET statement_timeout='5000ms'")
                    connection.execute(
                        f"UPDATE {ANALYTICS_SCHEMA}.scheduler_runtime SET "
                        f"heartbeat_at=clock_timestamp(),"
                        "available=%s WHERE singleton AND instance_id=%s",
                        (not leader.closed and collector.poll() is None, instance),
                    )
            except psycopg.Error:
                log.warning("Scheduler heartbeat database unavailable")

    thread = Thread(target=pulse, daemon=True, name="sync-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        if not leader.closed:
            leader.execute(
                f"UPDATE {ANALYTICS_SCHEMA}.scheduler_runtime SET available=false "
                "WHERE singleton AND instance_id=%s",
                (instance,),
            )
