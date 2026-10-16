"""Bounded, read-only observation of persisted background failures.

Only identifiers and timestamps leave a runtime; business payloads and provider
error strings are deliberately never selected. All support I/O runs off-thread.
"""

import logging
import os
import queue
import threading
import time
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

# table, identity, time, failed predicate: every SQL fragment is a fixed constant.
_ANALYTICS = (
    ("scheduled_sync_runs", "job", "finished_at", "status='failed'", "sync_failed"),
    ("manual_sync_requests", "request_id", "finished_at", "state='failed'", "sync_failed"),
)
_DOCUMENTS = tuple(
    (table, identity, "updated_at", predicate, "document_failed")
    for table, identity, predicate in (
        (
            "native_dispatch",
            "operation_id",
            "state IN ('rejected','unknown') OR "
            "(state IN ('ready','connecting') AND last_error IS NOT NULL)",
        ),
        (
            "commercial_invoice_dispatch",
            "document_id",
            "state IN ('rejected','unknown') OR "
            "(state IN ('ready','connecting') AND last_error IS NOT NULL)",
        ),
        (
            "commercial_counterparty_operations",
            "id",
            "state IN ('rejected','unknown') OR "
            "(state IN ('queued','connecting') AND error_code IS NOT NULL)",
        ),
    )
)


def read_failures(db, schemas):
    """Caller opens a read-only transaction with a short deadline."""
    for area, tables in (("analytics", _ANALYTICS), ("documents", _DOCUMENTS)):
        if area not in schemas:
            continue
        for table, identity, timestamp, predicate, code in tables:
            # Older deployments may not have commercial queues yet. Savepoints
            # isolate missing optional tables without weakening database grants.
            try:
                with db.transaction():
                    rows = db.execute(
                        sql.SQL(
                            "SELECT {id}::text AS identity,{timestamp} AS occurred_at "
                            "FROM {schema}.{table} WHERE ({predicate}) AND {timestamp} "
                            ">now()-interval '24 hours' ORDER BY {timestamp} DESC LIMIT 30"
                        ).format(
                            id=sql.Identifier(identity),
                            timestamp=sql.Identifier(timestamp),
                            schema=sql.Identifier(schemas[area]),
                            table=sql.Identifier(table),
                            predicate=sql.SQL(predicate),
                        )
                    ).fetchall()
            except psycopg.errors.UndefinedTable:
                continue
            for row in rows:
                yield table, row["identity"], row["occurred_at"], code
    if "documents" in schemas:
        yield from read_worker_health(db, schemas["documents"])


def read_worker_health(db, schema):
    """Observe only fixed presence flags; never fetch heartbeat JSON or errors.

    A worker rewrites heartbeat on every cycle without an incident identifier.
    One receipt per category and UTC hour prevents polls or observer restarts
    from manufacturing occurrences. Stale heartbeats are not liveness evidence.
    """
    try:
        with db.transaction():
            row = db.execute(
                sql.SQL(
                    "SELECT data ? 'telegram_error' AS telegram_error, "
                    "data ? 'commercial_error' AS commercial_error, "
                    "data ? 'counterparty_error' AS counterparty_error, "
                    "date_trunc('hour',updated_at,'UTC') AS occurred_at "
                    "FROM {}.native_jobs WHERE name='heartbeat' "
                    "AND updated_at>now()-interval '2 minutes' LIMIT 1"
                ).format(sql.Identifier(schema))
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        return
    if row:
        for category in ("telegram_error", "commercial_error", "counterparty_error"):
            if row[category]:
                yield "native_jobs", category, row["occurred_at"], "document_failed"


class BackgroundMonitor:
    def __init__(self, store, *, connect=psycopg.connect, retention=False):
        self.store, self.connect = store, connect
        self.retention = retention
        self.queue = queue.Queue(maxsize=64)
        self.pending = set()
        self.last_poll = {}
        self.last_prune = -3600
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="support-background", daemon=True)
        self.thread.start()

    def poll(self, context, dsn, schemas):
        key = (context.company_id, tuple(sorted(schemas)))
        with self.lock:
            if key in self.pending or time.monotonic() - self.last_poll.get(key, -60) < 60:
                return
            self.pending.add(key)
            try:
                self.queue.put_nowait(("poll", key, context, dsn, dict(schemas)))
            except queue.Full:
                self.pending.discard(key)

    def exited(self, context, occurrence):
        try:
            self.queue.put_nowait(("exit", context, occurrence))
        except queue.Full:
            pass

    def record(self, context, occurrence, code):
        key = uuid5(NAMESPACE_URL, "support-background:" + context.company_id + ":" + occurrence)
        self.store.event(
            context,
            {
                "client_request_id": str(key),
                "screen": "other",
                "status_code": None,
                "request_id": None,
                "diagnostic": {},
            },
            "background",
            code,
        )

    def scan(self, context, dsn, schemas):
        with self.connect(dsn, row_factory=dict_row, connect_timeout=2) as db:
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout='1000ms'")
            db.execute("SET LOCAL lock_timeout='250ms'")
            # Materialize then close the runtime transaction before central writes.
            failures = list(read_failures(db, schemas))
        for table, identity, occurred_at, code in failures:
            self.record(context, f"{table}:{identity}:{occurred_at.isoformat()}", code)

    def _run(self):
        while not self.stopped.is_set():
            try:
                item = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if self.retention and time.monotonic() - self.last_prune >= 3600:
                    self.last_prune = time.monotonic()
                    try:
                        self.store.prune()
                    except Exception:
                        log.warning("support_retention_unavailable")
                if item[0] == "poll":
                    self.scan(*item[2:])
                else:
                    self.record(item[1], item[2], "unhandled_exception")
            except Exception:
                log.warning("support_background_unavailable")
            finally:
                if item[0] == "poll":
                    with self.lock:
                        self.pending.discard(item[1])
                        self.last_poll[item[1]] = time.monotonic()
                        # Fleet size is bounded by configured companies; cap stale entries too.
                        if len(self.last_poll) > 2048:
                            self.last_poll.pop(next(iter(self.last_poll)))
                self.queue.task_done()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=0.5)


def start_primary():
    """Separate worker processes cannot reuse the portal's capture singleton."""
    if os.environ.get("RESTCONTROL_RUNTIME_MODE") == "tenant" or not os.environ.get(
        "SUPPORT_DATABASE_URL"
    ):
        return None
    try:
        from app.support.capture import configure_capture
        from app.support.models import Context
        from app.support.store import SupportStore

        context = Context(
            os.environ.get("SUPPORT_COMPANY_ID", "primary-dashboard"),
            os.environ.get("SUPPORT_COMPANY_NAME", "Чайка"),
            "system",
        )
        store = SupportStore(os.environ["SUPPORT_DATABASE_URL"])
        return configure_capture(store, context), BackgroundMonitor(store), context
    except Exception:
        log.warning("support_background_unavailable")
        return None


def capture_failure(code, *, exception=None):
    try:
        from app.support.capture import capture_background

        capture_background(code, exception=exception)
    except Exception:
        log.warning("support_background_unavailable")


def close_primary(support):
    if support:
        support[0].close()
        support[1].close()
