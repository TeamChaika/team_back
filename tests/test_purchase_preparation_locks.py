"""Exercise the real preparation entry points with concurrent scoped PostgreSQL connections."""

import os
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from test_reference_sync import db as db

from app import purchase_impact_precompute, purchase_prices_precompute


@pytest.mark.parametrize(
    ("preparation", "table", "legacy_key"),
    [
        (purchase_impact_precompute, "purchase_impact_prepared", 7623011071800),
        (purchase_prices_precompute, "purchase_prices_prepared", 7623011071900),
    ],
)
def test_preparation_lock_follows_the_publication_table(
    db, monkeypatch, preparation, table, legacy_key
):
    # The db fixture enforces the isolated localhost DSN. All connections use the
    # ordinary backend role and the grants installed by the project's migrations.
    settings = SimpleNamespace(
        database_url=SecretStr(
            make_conninfo(os.environ["CHAIKA_TEST_DATABASE_URL"], options="-c role=chaika_backend")
        )
    )
    target = sql.Identifier("chaika", table)
    with db.transaction(force_rollback=True):
        db.execute(sql.SQL("DELETE FROM {} WHERE false").format(target))
        assert preparation.ensure_prepared(settings) == {"status": "busy"}

    # Model a legacy worker in another schema holding both its global key and
    # a write lock on its own relation, which has exactly the same table name.
    db.execute("SELECT pg_advisory_xact_lock(%s)", (legacy_key,))
    db.execute(sql.SQL("CREATE TEMP TABLE {} (id integer)").format(sql.Identifier(table)))
    db.execute(
        sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
            sql.Identifier("pg_temp", table)
        )
    )
    db.execute("SET LOCAL lock_timeout='1s'")

    class ProbeFinished(Exception):
        pass

    def check_during_preparation(writer, **kwargs):
        assert writer.execute("SELECT current_user AS role").fetchone()["role"] == "chaika_backend"
        # Read the original table while the new writer owns it. Two new writers
        # cannot build at once, and an old publisher's DML must also wait.
        db.execute(sql.SQL("SELECT id FROM {}").format(target)).fetchall()
        assert preparation.ensure_prepared(settings) == {"status": "busy"}
        with pytest.raises(psycopg.errors.LockNotAvailable), db.transaction():
            db.execute(sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE NOWAIT").format(target))
        raise ProbeFinished

    monkeypatch.setattr(preparation, "source_revision", check_during_preparation)
    with pytest.raises(ProbeFinished):
        preparation.ensure_prepared(settings)
    # The failed preparation releases its relation lock at transaction rollback.
    db.execute(sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE NOWAIT").format(target))
