"""Real ledger transactions in a dedicated, explicitly selected loopback database."""

import os
from datetime import date
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr

from app import low_markup_delivery as delivery
from app.documents.telegram_cleanup import TelegramError
from app.low_markup_report import Report

VENUE, ACTOR = UUID(int=11), UUID(int=12)
DAY = date(2026, 10, 19)


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("LOW_MARKUP_TEST_DSN")
    if not dsn:
        pytest.skip("LOW_MARKUP_TEST_DSN required")
    if conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("Only loopback PostgreSQL allowed")
    with psycopg.connect(dsn, autocommit=True) as admin:
        if not admin.execute(
            "SELECT 1 FROM pg_database WHERE datname='low_markup_tests'"
        ).fetchone():
            admin.execute("CREATE DATABASE low_markup_tests")
        for role in ("anon", "authenticated", "chaika_backend"):
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    dsn = make_conninfo(dsn, dbname="low_markup_tests")
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as db:
        db.execute("DROP SCHEMA IF EXISTS chaika CASCADE")
        db.execute("CREATE SCHEMA chaika")
        db.execute("GRANT USAGE ON SCHEMA chaika TO chaika_backend,anon,authenticated")
        db.execute(
            "CREATE TABLE chaika.corporate_nodes (source_id text,id uuid,name text,type text)"
        )
        db.execute(
            "INSERT INTO chaika.corporate_nodes VALUES ('primary',%s,'Venue','DEPARTMENT')",
            (VENUE,),
        )
        db.execute(
            "CREATE TABLE chaika.operation_settings(singleton boolean,revision integer,"
            "settings jsonb,updated_at timestamptz,updated_by uuid)"
        )
        db.execute(
            Path(__file__)
            .parents[1]
            .joinpath("supabase/migrations/20261020170000_low_markup_reports.sql")
            .read_text()
        )
        db.execute(
            "INSERT INTO chaika.low_markup_destinations(department_id,chat_id,chat_title,"
            "bot_id,enabled,updated_by) VALUES(%s,-123,'Group','123',true,%s)",
            (VENUE, ACTOR),
        )
        yield db, SimpleNamespace(database_url=SecretStr(dsn))


@pytest.fixture
def mocked(monkeypatch):
    bot = Mock()
    bot.call.return_value = {"message_id": 77}
    monkeypatch.setattr(delivery, "Telegram", lambda *a, **k: bot)
    monkeypatch.setattr(
        delivery,
        "DocumentSettings",
        lambda: SimpleNamespace(bot_token=SecretStr("123:secret"), telegram_local_address=""),
    )
    prepare = Mock(return_value={str(VENUE): Report("Venue", DAY, (), 0)})
    monkeypatch.setattr(delivery, "prepare_report", prepare)
    return bot, prepare


def test_committed_before_send_dedup_and_restart(database, mocked):
    db, settings = database
    bot, _ = mocked

    def send(*args, **kwargs):
        with delivery.connect(settings) as reader:
            assert (
                reader.execute("SELECT state FROM chaika.low_markup_deliveries").fetchone()["state"]
                == "sending"
            )
        return {"message_id": 77}

    bot.call.side_effect = send
    delivery.run_reports(settings, DAY, Event())
    delivery.run_reports(settings, DAY, Event())
    assert bot.call.call_count == 1
    db.execute("UPDATE chaika.low_markup_deliveries SET state='sending'")
    with pytest.raises(delivery.SyncError):
        delivery.run_reports(settings, DAY, Event())
    assert (
        db.execute("SELECT state FROM chaika.low_markup_deliveries").fetchone()["state"]
        == "unknown"
    )
    assert bot.call.call_count == 1


def test_rate_limit_durable_retry(database, mocked):
    db, settings = database
    bot, _ = mocked
    bot.call.side_effect = TelegramError(429, retry_after=60)
    with pytest.raises(delivery.SyncError):
        delivery.run_reports(settings, DAY, Event())
    with pytest.raises(delivery.SyncError):
        delivery.run_reports(settings, DAY, Event())
    assert bot.call.call_count == 1
    db.execute("UPDATE chaika.low_markup_deliveries SET next_attempt_at=now()-interval '1 second'")
    bot.call.side_effect = None
    delivery.run_reports(settings, DAY, Event())
    assert db.execute("SELECT attempts,state FROM chaika.low_markup_deliveries").fetchone() == dict(
        attempts=2, state="sent"
    )


def test_concurrent_leader_and_rls(database, mocked):
    db, settings = database
    bot, _ = mocked
    db.execute("SELECT pg_advisory_lock(hashtextextended('chaika:low-markup-reports',0))")
    try:
        with pytest.raises(delivery.SyncError, match="low_markup_busy"):
            delivery.run_reports(settings, DAY, Event())
    finally:
        db.execute("SELECT pg_advisory_unlock(hashtextextended('chaika:low-markup-reports',0))")
    bot.call.assert_not_called()
    db.execute("SET ROLE anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM chaika.low_markup_destinations")
    finally:
        db.execute("RESET ROLE")
    db.execute("SET ROLE chaika_backend")
    try:
        assert (
            db.execute("SELECT count(*) AS n FROM chaika.low_markup_destinations").fetchone()["n"]
            == 1
        )
    finally:
        db.execute("RESET ROLE")


def test_incomplete_source_never_sends_or_reserves(database, mocked):
    db, settings = database
    bot, prepare = mocked
    prepare.side_effect = ValueError("low_markup_incomplete_source")
    with pytest.raises(ValueError, match="incomplete_source"):
        delivery.run_reports(settings, DAY, Event())
    bot.call.assert_not_called()
    assert db.execute("SELECT count(*) AS n FROM chaika.low_markup_deliveries").fetchone()["n"] == 0


def test_caption_matches_strict_threshold():
    text = delivery.caption(Report("Venue", DAY, (), 1))
    assert "менее 150%" in text
    assert "100%" not in text
    assert "себестоимости: 1" in text
