from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.support_background import BackgroundMonitor


class DB:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        yield

    def execute(self, statement):
        self.queries.append(statement)
        return self

    def fetchall(self):
        return [{"identity": "safe-id", "occurred_at": datetime(2026, 10, 16, tzinfo=UTC)}]


def test_scan_is_readonly_safe_and_replay_uses_stable_receipts():
    db, store = DB(), Mock()
    monitor = BackgroundMonitor(store, connect=lambda *args, **kwargs: db)
    context = SimpleNamespace(company_id="company-one", company_name="One", actor_id="system")
    try:
        monitor.scan(context, "private-dsn", {"analytics": "company_analytics"})
        monitor.scan(context, "private-dsn", {"analytics": "company_analytics"})
    finally:
        monitor.close()
    assert db.queries[0] == "SET TRANSACTION READ ONLY"
    calls = store.event.call_args_list
    assert calls[0] == calls[2] and calls[1] == calls[3]
    assert all(call.args[0] is context for call in calls)
    assert all(call.args[1]["diagnostic"] == {} for call in calls)
    assert "private-dsn" not in repr(calls)
    selects = [q.as_string() for q in db.queries if not isinstance(q, str)]
    assert all("LIMIT 30" in q and "DESC" in q and "24 hours" in q for q in selects)
    assert all("SELECT *" not in q and "requested_by" not in q for q in selects)


def test_changed_failure_is_a_new_occurrence_and_company_isolated():
    store = Mock()
    monitor = BackgroundMonitor(store)
    try:
        a = SimpleNamespace(company_id="a", actor_id="system")
        b = SimpleNamespace(company_id="b", actor_id="system")
        monitor.record(a, "job:finished-one", "sync_failed")
        monitor.record(a, "job:finished-two", "sync_failed")
        monitor.record(b, "job:finished-one", "sync_failed")
        assert len({c.args[1]["client_request_id"] for c in store.event.call_args_list}) == 3
    finally:
        monitor.close()


def test_worker_failure_does_not_escape_or_stop_next_event():
    store = Mock()
    store.event.side_effect = [RuntimeError("secret password"), None]
    monitor = BackgroundMonitor(store)
    try:
        context = SimpleNamespace(company_id="a", actor_id="system")
        monitor.exited(context, "first")
        monitor.exited(context, "second")
        monitor.queue.join()
        assert store.event.call_count == 2
        assert monitor.thread.is_alive()
    finally:
        monitor.close()


def test_primary_is_disabled_in_tenant_child(monkeypatch):
    from app.support_background import start_primary

    monkeypatch.setenv("RESTCONTROL_RUNTIME_MODE", "tenant")
    monkeypatch.setenv("SUPPORT_DATABASE_URL", "must-never-be-used")
    assert start_primary() is None


def test_fleet_observer_validates_company_and_uses_only_own_runtime_role():
    from uuid import UUID

    from app.saas_admin.support_monitor import observe

    company_id = UUID(int=99)
    company = {"id": str(company_id), "name": "Example"}
    monitor = Mock()
    config = {
        "company_id": str(company_id),
        "runtime_dsn": f"host=localhost user=c_{company_id.hex}_runtime",
    }
    context = observe(monitor, config, company)
    assert context.actor_id == "system"
    assert monitor.poll.call_args.args[1] == config["runtime_dsn"]
    assert set(monitor.poll.call_args.args[2]) == {"analytics", "documents", "payments"}
    config["runtime_dsn"] = "host=localhost user=postgres"
    with pytest.raises(ValueError):
        observe(monitor, config, company)
    config["company_id"] = str(UUID(int=100))
    with pytest.raises(ValueError):
        observe(monitor, config, company)
    assert monitor.poll.call_count == 1


@pytest.mark.parametrize(
    "flags,expected",
    [
        ({}, []),
        ({"telegram_error": True}, ["telegram_error"]),
        (
            {"commercial_error": True, "counterparty_error": True},
            ["commercial_error", "counterparty_error"],
        ),
    ],
)
def test_heartbeat_reads_fixed_flags_only(flags, expected):
    from app.support_background import read_worker_health

    class HeartbeatDB(DB):
        def fetchone(self):
            return (
                dict(
                    telegram_error=False,
                    commercial_error=False,
                    counterparty_error=False,
                    occurred_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
                )
                | flags
            )

    db = HeartbeatDB()
    events = list(read_worker_health(db, "tenant_documents"))
    assert [event[1] for event in events] == expected
    assert all(event[0] == "native_jobs" and event[3] == "document_failed" for event in events)
    query = db.queries[0].as_string()
    assert "SELECT data," not in query and "data::text" not in query
    assert "2 minutes" in query and "LIMIT 1" in query
    assert "date_trunc('hour'" in query


def test_heartbeat_repeated_cycle_errors_share_one_receipt():
    from app.support_background import read_worker_health

    class HeartbeatDB(DB):
        def fetchone(self):
            return {
                "telegram_error": True,
                "commercial_error": False,
                "counterparty_error": False,
                "occurred_at": datetime(2026, 10, 16, 12, tzinfo=UTC),
            }

    store = Mock()
    monitor = BackgroundMonitor(store)
    context = SimpleNamespace(company_id="a", actor_id="system")
    try:
        for _ in range(3):
            for table, category, occurred_at, code in read_worker_health(
                HeartbeatDB(), "tenant_documents"
            ):
                monitor.record(context, f"{table}:{category}:{occurred_at.isoformat()}", code)
        assert len({c.args[1]["client_request_id"] for c in store.event.call_args_list}) == 1
        assert all(c.args[1]["diagnostic"] == {} for c in store.event.call_args_list)
    finally:
        monitor.close()


def test_missing_or_stale_heartbeat_creates_no_event():
    from app.support_background import read_worker_health

    class MissingDB(DB):
        def fetchone(self):
            return None

    assert list(read_worker_health(MissingDB(), "tenant_documents")) == []


def test_heartbeat_actual_persisted_shape_readonly_postgres():
    import os
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from app.support_background import read_worker_health

    dsn = os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CHAIKA_TEST_DATABASE_URL for isolated PostgreSQL")
    schema = "support_probe_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as db:
        try:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            db.execute(
                sql.SQL(
                    "CREATE TABLE {}.native_jobs("
                    "name text PRIMARY KEY,data jsonb,updated_at timestamptz)"
                ).format(sql.Identifier(schema))
            )
            for health, expected in [
                ({}, 0),
                ({"telegram_error": "ConnectError", "telegram_reason": "dns"}, 1),
                ({"commercial_error": "HTTPError", "counterparty_error": "TimeoutError"}, 2),
            ]:
                db.execute(
                    sql.SQL(
                        "INSERT INTO {}.native_jobs VALUES ('heartbeat',%s,now()) "
                        "ON CONFLICT(name) DO UPDATE SET "
                        "data=excluded.data,updated_at=excluded.updated_at"
                    ).format(sql.Identifier(schema)),
                    (Jsonb(health),),
                )
                with db.transaction():
                    db.execute("SET TRANSACTION READ ONLY")
                    rows = list(read_worker_health(db, schema))
                    assert len(rows) == expected
                    assert not any(
                        value in repr(rows)
                        for value in ("ConnectError", "HTTPError", "TimeoutError", "dns")
                    )
            db.execute(
                sql.SQL("UPDATE {}.native_jobs SET updated_at=now()-interval '10 minutes'").format(
                    sql.Identifier(schema)
                )
            )
            with db.transaction():
                db.execute("SET TRANSACTION READ ONLY")
                assert not list(read_worker_health(db, schema))
        finally:
            db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_dispatch_failure_queries_exclude_completed_and_obsolete_rows():
    from app.support_background import _DOCUMENTS

    assert {row[0] for row in _DOCUMENTS} == {
        "native_dispatch",
        "commercial_invoice_dispatch",
        "commercial_counterparty_operations",
    }
    for table, _, _, predicate, _ in _DOCUMENTS:
        assert "state IN ('rejected','unknown')" in predicate
        assert "'connecting'" in predicate
        assert (
            "'queued'" in predicate
            if table == "commercial_counterparty_operations"
            else "'ready'" in predicate
        )
        assert all(
            f"'{state}'" not in predicate
            for state in ("sent", "accepted", "processed", "obsolete", "confirmed")
        )
