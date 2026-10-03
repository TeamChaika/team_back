"""Real PostgreSQL tests in a fresh disposable database, never production."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tools import require_password_change as backfill

MIGRATION = Path(__file__).resolve().parents[1] / (
    "supabase/migrations/20261003150000_password_change_required.sql"
)
PASSWORD = "temporary-test-only"


@pytest.fixture
def database():
    base = os.environ.get("CHAIKA_PASSWORD_BACKFILL_TEST_DSN") or os.environ.get(
        "CHAIKA_TEST_DATABASE_URL"
    )
    if not base:
        pytest.skip("CHAIKA_PASSWORD_BACKFILL_TEST_DSN must target disposable port 15438")
    info = conninfo_to_dict(base)
    if info.get("host") != "127.0.0.1" or info.get("port") != "15438":
        pytest.fail("Only the isolated PostgreSQL at 127.0.0.1:15438 is allowed")
    name = f"password_backfill_test_{uuid4().hex}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(base, dbname=name, application_name="password-backfill-test")
    allowed = [f"127.0.0.1:15438/{name}"]
    try:
        with psycopg.connect(dsn) as db:
            db.execute("CREATE SCHEMA auth; CREATE SCHEMA chaika; CREATE SCHEMA extensions")
            db.execute("CREATE EXTENSION pgcrypto SCHEMA extensions")
            db.execute("CREATE TABLE auth.users(id uuid PRIMARY KEY, encrypted_password text)")
            db.execute("""
                CREATE TABLE chaika.web_users (
                    id uuid PRIMARY KEY REFERENCES auth.users(id),
                    active boolean NOT NULL, role text NOT NULL,
                    telegram_id bigint, grants jsonb NOT NULL DEFAULT '[]'
                )
            """)
            for i, value in enumerate((PASSWORD, PASSWORD, "personal-test-only"), start=1):
                db.execute(
                    """
                    INSERT INTO auth.users VALUES (%s, extensions.crypt(%s,
                        extensions.gen_salt('bf', 4)))
                """,
                    (f"00000000-0000-0000-0000-{i:012d}", value),
                )
            db.execute("""
                INSERT INTO auth.users VALUES
                    ('00000000-0000-0000-0000-000000000004', '$argon2id$unsupported'),
                    ('00000000-0000-0000-0000-000000000005', NULL)
            """)
            db.execute("""
                INSERT INTO chaika.web_users (id, active, role, telegram_id, grants)
                SELECT id, id != '00000000-0000-0000-0000-000000000002',
                    'manager', 123, '["warehouse-only"]' FROM auth.users
            """)
            db.execute(MIGRATION.read_text())
        yield dsn, allowed
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def run(database, **kwargs):
    dsn, allowed = database
    return backfill.run_backfill(dsn, PASSWORD, allowed_targets=allowed, **kwargs)


def test_actual_migration_preserves_flags_and_defaults_new_users_to_required(database):
    dsn, _ = database
    with psycopg.connect(dsn) as db:
        assert db.execute(
            "SELECT count(*) FROM chaika.web_users WHERE password_change_required"
        ).fetchone() == (0,)
        db.execute("UPDATE chaika.web_users SET password_change_required=true WHERE NOT active")
        db.execute(MIGRATION.read_text())
        assert db.execute(
            "SELECT count(*) FROM chaika.web_users WHERE password_change_required"
        ).fetchone() == (1,)
        new_id = uuid4()
        db.execute("INSERT INTO auth.users VALUES (%s, NULL)", (new_id,))
        db.execute(
            "INSERT INTO chaika.web_users(id,active,role) VALUES(%s,true,'manager')", (new_id,)
        )
        assert db.execute(
            "SELECT password_change_required, password_changed_at FROM "
            "chaika.web_users WHERE id=%s",
            (new_id,),
        ).fetchone() == (True, None)


def test_dry_run_and_apply_only_matching_users_preserve_permissions_auth_and_telegram(database):
    dsn, _ = database
    with psycopg.connect(dsn) as db:
        before = db.execute(
            "SELECT "
            "w.id,w.active,w.role,w.telegram_id,w.grants,a.encrypted_password "
            "FROM chaika.web_users w JOIN auth.users a USING(id) ORDER BY id"
        ).fetchall()
    result = run(database)
    assert result == dict(
        mode="read_only",
        total=5,
        missing_auth=0,
        unsupported=2,
        matches=2,
        to_flag=2,
        already_flagged=0,
        updated=0,
    )
    with psycopg.connect(dsn) as db:
        assert db.execute(
            "SELECT count(*) FROM chaika.web_users WHERE password_change_required"
        ).fetchone() == (0,)
    result = run(database, apply=True, expect_matches=2)
    assert result["updated"] == 2
    with psycopg.connect(dsn) as db:
        after = db.execute(
            "SELECT "
            "w.id,w.active,w.role,w.telegram_id,w.grants,a.encrypted_password "
            "FROM chaika.web_users w JOIN auth.users a USING(id) ORDER BY id"
        ).fetchall()
        assert after == before
        assert db.execute(
            "SELECT active,password_change_required FROM chaika.web_users ORDER BY id"
        ).fetchall() == [(True, True), (False, True), (True, False), (True, False), (True, False)]
    assert run(database, apply=True, expect_matches=2)["updated"] == 0


def test_stale_plan_rolls_back_and_bcrypt_variants_work(database):
    dsn, _ = database
    with pytest.raises(backfill.BackfillError, match="Match count changed"):
        run(database, apply=True, expect_matches=3)
    assert run(database)["to_flag"] == 2
    for prefix in ("$2b$", "$2y$"):
        with psycopg.connect(dsn) as db:
            db.execute(
                "UPDATE auth.users SET encrypted_password=%s || "
                "substring(encrypted_password FROM 5) WHERE "
                "id='00000000-0000-0000-0000-000000000001'",
                (prefix,),
            )
        assert run(database)["matches"] == 2


def test_password_changed_while_apply_waits_is_rechecked(database):
    dsn, _ = database
    with psycopg.connect(dsn) as changing, ThreadPoolExecutor(max_workers=1) as pool:
        changing.execute(
            "UPDATE auth.users SET "
            "encrypted_password=extensions.crypt('new-personal', "
            "extensions.gen_salt('bf',4)) WHERE "
            "id='00000000-0000-0000-0000-000000000001'"
        )
        future = pool.submit(run, database, apply=True, expect_matches=2)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with psycopg.connect(dsn, autocommit=True) as observer:
                    waiting = observer.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "datname=current_database() AND wait_event_type='Lock'"
                    ).fetchone()[0]
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting
            changing.execute(
                "UPDATE chaika.web_users SET password_change_required=false, "
                "password_changed_at=now() WHERE "
                "id='00000000-0000-0000-0000-000000000001'"
            )
        finally:
            changing.commit()
        with pytest.raises(backfill.BackfillError, match="Match count changed"):
            future.result(timeout=5)
    assert run(database)["matches"] == 1
    run(database, apply=True, expect_matches=1)
    with psycopg.connect(dsn) as db:
        assert db.execute(
            "SELECT password_change_required,password_changed_at IS NOT NULL FROM "
            "chaika.web_users WHERE id='00000000-0000-0000-0000-000000000001'"
        ).fetchone() == (False, True)


def test_cli_never_displays_database_error_or_secrets(monkeypatch, capsys):
    monkeypatch.setenv("CHAIKA_PASSWORD_BACKFILL_ALLOWED_TARGETS", '["127.0.0.1:15438/test"]')
    monkeypatch.setenv("CHAIKA_PASSWORD_BACKFILL_DSN", "dsn-secret")
    monkeypatch.setenv("CHAIKA_TEMPORARY_PASSWORD", "password-secret")

    def fail(*args, **kwargs):
        raise RuntimeError("dsn-secret password-secret $2a$hash-secret")

    monkeypatch.setattr(backfill, "run_backfill", fail)
    assert backfill.main([]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "secret" not in captured.err
    assert "Traceback" not in captured.err


def test_target_requires_exact_allowlist(monkeypatch):
    for key in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(backfill.BackfillError, match="allowlist"):
        backfill.validate_target("host=127.0.0.1 port=15438 dbname=wrong", ["127.0.0.1:15438/test"])
    with pytest.raises(backfill.BackfillError, match="Indirect"):
        backfill.validate_target(
            "host=127.0.0.1 hostaddr=1.2.3.4 port=15438 dbname=test", ["127.0.0.1:15438/test"]
        )


def test_auth_change_waits_for_apply_then_can_clear_flag(database, monkeypatch):
    dsn, _ = database
    locked, proceed = Event(), Event()
    original = backfill.classify

    def paused_classify(*args):
        locked.set()
        assert proceed.wait(5)
        return original(*args)

    def change_password():
        with psycopg.connect(dsn) as db:
            db.execute(
                "UPDATE auth.users SET encrypted_password=extensions.crypt(%s, "
                "extensions.gen_salt('bf',4)) WHERE "
                "id='00000000-0000-0000-0000-000000000001'",
                ("new-personal",),
            )
            db.execute(
                "UPDATE chaika.web_users SET password_change_required=false, "
                "password_changed_at=now() WHERE "
                "id='00000000-0000-0000-0000-000000000001'"
            )

    monkeypatch.setattr(backfill, "classify", paused_classify)
    with ThreadPoolExecutor(max_workers=2) as pool:
        applying = pool.submit(run, database, apply=True, expect_matches=2)
        try:
            assert locked.wait(5)
            changing = pool.submit(change_password)
            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                with psycopg.connect(dsn, autocommit=True) as db:
                    waiting = db.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "datname=current_database() AND wait_event_type='Lock'"
                    ).fetchone()[0]
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting
        finally:
            proceed.set()
        assert applying.result(timeout=5)["updated"] == 2
        changing.result(timeout=5)
    with psycopg.connect(dsn) as db:
        assert db.execute(
            "SELECT password_change_required,password_changed_at IS NOT NULL FROM "
            "chaika.web_users WHERE id='00000000-0000-0000-0000-000000000001'"
        ).fetchone() == (False, True)
