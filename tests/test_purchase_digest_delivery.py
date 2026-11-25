"""Synthetic delivery tests. PostgreSQL cases use the existing disposable loopback fixture."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from test_low_markup_postgres import ACTOR, VENUE
from test_low_markup_postgres import database as database

from app import purchase_digest_delivery as delivery
from app import purchase_digest_report
from app.documents.telegram_cleanup import TelegramError

DAY = date(2026, 10, 19)


def test_cutoff_is_current_report_date_from_0900_moscow():
    before = datetime(2026, 10, 19, 5, 59, 59, tzinfo=UTC)
    assert delivery.through_date(before) == DAY - timedelta(days=1)
    assert delivery.through_date(before + timedelta(seconds=1)) == DAY


@pytest.fixture
def digest(database, monkeypatch):
    db, settings = database
    migrations = Path(__file__).parents[1] / "supabase/migrations"
    migration = migrations / "20261125010000_purchase_digest_reports.sql"
    db.execute(migration.read_text())
    db.execute(
        "INSERT INTO chaika.purchase_digest_destinations(department_id,chat_id,chat_title,"
        "bot_id,enabled,start_date,updated_by) VALUES(%s,-123,'Group','123',true,%s,%s)",
        (VENUE, DAY, ACTOR),
    )
    bot = Mock()
    bot.call.return_value = {"message_id": 77}
    build = Mock(return_value=b"PK synthetic")
    monkeypatch.setattr(purchase_digest_report, "build_report", build)
    monkeypatch.setattr(delivery, "Telegram", lambda *a, **kw: bot)
    monkeypatch.setattr(
        delivery, "DocumentSettings",
        lambda: SimpleNamespace(bot_token=SecretStr("123:secret"), telegram_local_address=""),
    )
    return db, settings, bot, build


def test_catch_up_with_committed_claim_and_no_duplicate(digest):
    db, settings, bot, build = digest

    def send(*args, **kwargs):
        assert db.execute(
            "SELECT count(*) AS n FROM chaika.purchase_digest_deliveries WHERE state='sending'"
        ).fetchone()["n"] == 1
        assert kwargs["chat_id"] == -123
        assert kwargs["document"][1] == b"PK synthetic"
        return {"message_id": 77}

    bot.call.side_effect = send
    end = DAY + timedelta(days=1)
    delivery.run_reports(settings, end, Event())
    delivery.run_reports(settings, end, Event())
    assert [call.args[2] for call in build.call_args_list] == [DAY, end]
    assert bot.call.call_count == 2


def test_timeout_only_blocks_affected_day_and_crash_is_not_retried(digest):
    db, settings, bot, _ = digest
    bot.call.side_effect = [httpx.ReadTimeout("uncertain"), {"message_id": 9}]
    end = DAY + timedelta(days=1)
    delivery.run_reports(settings, end, Event())
    assert db.execute(
        "SELECT state FROM chaika.purchase_digest_deliveries ORDER BY business_date"
    ).fetchall() == [{"state": "unknown"}, {"state": "sent"}]
    db.execute("UPDATE chaika.purchase_digest_deliveries SET state='sending' WHERE state='unknown'")
    delivery.run_reports(settings, end, Event())
    assert bot.call.call_count == 2
    assert db.execute(
        "SELECT error_code FROM chaika.purchase_digest_deliveries WHERE business_date=%s", (DAY,)
    ).fetchone()["error_code"] == "interrupted"


def test_rate_limited_attempt_waits_then_can_retry(digest):
    db, settings, bot, _ = digest
    bot.call.side_effect = TelegramError(429, retry_after=60)
    delivery.run_reports(settings, DAY, Event())
    delivery.run_reports(settings, DAY, Event())
    assert bot.call.call_count == 1
    db.execute("UPDATE chaika.purchase_digest_deliveries SET next_attempt_at=now()")
    bot.call.side_effect = None
    delivery.run_reports(settings, DAY, Event())
    assert db.execute(
        "SELECT state,attempts FROM chaika.purchase_digest_deliveries"
    ).fetchone() == {"state": "sent", "attempts": 2}


def test_route_change_during_generation_cancels_old_destination(digest):
    db, settings, bot, build = digest
    def redirect(*_):
        db.execute(
            "UPDATE chaika.purchase_digest_destinations SET chat_id=-456, revision=2, "
            "start_date=%s", (DAY + timedelta(days=1),)
        )
        return b"PK synthetic"

    build.side_effect = redirect
    delivery.run_reports(settings, DAY, Event())
    delivery.run_reports(settings, DAY, Event())
    bot.call.assert_not_called()
    build.side_effect = None
    delivery.run_reports(settings, DAY + timedelta(days=1), Event())
    assert bot.call.call_args.kwargs["chat_id"] == -456

