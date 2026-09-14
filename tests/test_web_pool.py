"""Live isolated-Postgres checks for connection reuse and fresh access decisions."""

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from test_reference_sync import bundle as bundle
from test_reference_sync import db as db
from test_reference_sync import stage

from app.core.config import Settings
from app.sync_references import publish
from app.web.repository import Repository


@pytest.fixture
def pooled_repository():
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires isolated PostgreSQL")
    if "127.0.0.1:15438/" not in url:
        pytest.fail("DB tests must use isolated localhost:15438")
    repo = Repository(Settings(_env_file=None, database_url=url))
    try:
        repo.open()
        yield repo
    finally:
        repo.close()


def test_pool_reuses_connections_and_preserves_read_only_after_error(pooled_repository):
    repo = pooled_repository
    pids = set()
    for _ in range(5):
        with repo.connection() as connection:
            pids.add(connection.info.backend_pid)
            assert connection.execute("SHOW transaction_read_only").fetchone() == {
                "transaction_read_only": "on"
            }
            assert connection.execute("SHOW statement_timeout").fetchone() == {
                "statement_timeout": "15s"
            }
    assert len(pids) <= 2
    with repo._pool.connection() as connection:
        assert connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert (
            connection.execute("SHOW default_transaction_read_only").fetchone()[
                "default_transaction_read_only"
            ]
            == "off"
        )
        assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), repo.connection() as connection:
        connection.execute("UPDATE chaika.web_users SET active=active")
    with repo.connection() as connection:
        assert connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        assert (
            connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            == "on"
        )


def test_pool_replaces_closed_connection(pooled_repository):
    repo = pooled_repository
    with pytest.raises(psycopg.OperationalError), repo.connection() as connection:
        closed_pid = connection.info.backend_pid
        connection.close()
    with repo.connection() as connection:
        assert connection.info.backend_pid != closed_pid
        assert connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_batched_scope_rechecks_user_grants_and_active_state(db, bundle):
    sources, snapshots = stage(db, bundle)
    publish(db, sources, snapshots)
    department = db.execute("SELECT id FROM chaika.corporate_nodes LIMIT 1").fetchone()[0]
    user_id = uuid4()
    db.execute("SET LOCAL ROLE postgres")
    db.execute("INSERT INTO auth.users(id) VALUES(%s)", (user_id,))
    db.execute(
        "INSERT INTO chaika.web_users(id,display_name,role) VALUES(%s,'Test','manager')",
        (user_id,),
    )
    db.execute(
        "INSERT INTO chaika.web_department_access VALUES(%s,'primary',%s)",
        (user_id, department),
    )
    db.execute("SET LOCAL ROLE chaika_backend")
    repo = Repository(Settings(_env_file=None))

    @contextmanager
    def connection():
        factory = db.row_factory
        db.row_factory = dict_row
        try:
            yield db
        finally:
            db.row_factory = factory

    repo.connection = connection
    assert repo.scope(user_id).ids == [department]
    assert repo.metadata(repo.scope(user_id))["user"]["role"] == "manager"
    with pytest.raises(HTTPException) as denied:
        repo.scope(user_id, uuid4())
    assert denied.value.status_code == 403
    db.execute("SET LOCAL ROLE postgres")
    db.execute("DELETE FROM chaika.web_department_access WHERE user_id=%s", (user_id,))
    db.execute("SET LOCAL ROLE chaika_backend")
    with pytest.raises(HTTPException) as revoked:
        repo.scope(user_id)
    assert revoked.value.status_code == 403
    db.execute("SET LOCAL ROLE postgres")
    db.execute("UPDATE chaika.web_users SET role='owner' WHERE id=%s", (user_id,))
    db.execute("SET LOCAL ROLE chaika_backend")
    assert repo.scope(user_id).unrestricted
    db.execute("SET LOCAL ROLE postgres")
    db.execute("UPDATE chaika.web_users SET active=false WHERE id=%s", (user_id,))
    db.execute("SET LOCAL ROLE chaika_backend")
    with pytest.raises(HTTPException) as disabled:
        repo.scope(user_id)
    assert disabled.value.status_code == 403


def test_stock_snapshot_isolation_is_transaction_local(pooled_repository):
    repo = pooled_repository
    with repo.connection(repeatable=True) as connection:
        assert (
            connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
            == "repeatable read"
        )
        assert (
            connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            == "on"
        )
    with repo.connection() as connection:
        assert (
            connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
            == "read committed"
        )
