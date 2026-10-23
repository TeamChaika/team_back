from contextlib import nullcontext
from datetime import date
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from app import low_markup_delivery as delivery
from app.documents.telegram import Telegram, handle_update
from app.documents.telegram_cleanup import TelegramError
from app.low_markup_report import Report

DAY = date(2026, 10, 19)
ID = "11111111-1111-4111-8111-111111111111"
TARGET = dict(department_id=ID, chat_id=-100123, bot_id="123", name="Venue")


def test_caption_matches_report_threshold():
    text = delivery.caption(Report("Venue", DAY, (), 2))
    assert "150%" in text and "100%" not in text
    assert "себестоимости: 2" in text


class Database:
    def __init__(self, prior=()):
        self.prior = prior
        self.writes = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        text = query.as_string() if hasattr(query, "as_string") else query
        self.writes.append((text, params))
        return SimpleNamespace(fetchall=lambda: self.prior)


@pytest.fixture
def configured(monkeypatch):
    bot = Mock()
    bot.call.return_value = {"message_id": 42}
    monkeypatch.setattr(delivery, "destinations", lambda db: [TARGET])
    monkeypatch.setattr(
        delivery,
        "DocumentSettings",
        lambda: SimpleNamespace(bot_token=SecretStr("123:secret"), telegram_local_address=""),
    )
    monkeypatch.setattr(delivery, "Telegram", lambda *args, **kwargs: bot)
    prepare = Mock(return_value={ID: Report("Venue", DAY, (), 2)})
    monkeypatch.setattr(delivery, "prepare_report", prepare)
    return bot, prepare


def test_disabled_does_not_load_credentials_or_reports(monkeypatch):
    monkeypatch.setattr(delivery, "destinations", lambda db: [])
    forbidden = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(delivery, "DocumentSettings", forbidden)
    monkeypatch.setattr(delivery, "prepare_report", forbidden)
    delivery._run(Database(), object(), DAY, Event())


def test_sent_and_unknown_are_never_resent(configured):
    bot, prepare = configured
    for state in ("sent", "unknown"):
        db = Database([dict(department_id=ID, state=state, due=True)])
        if state == "unknown":
            with pytest.raises(delivery.SyncError):
                delivery._run(db, object(), DAY, Event())
        else:
            delivery._run(db, object(), DAY, Event())
    bot.call.assert_not_called()
    prepare.assert_not_called()


def test_durable_sending_precedes_http_and_empty_report_sent(configured):
    bot, prepare = configured
    db = Database()

    def send(*args, **kwargs):
        assert any("INSERT INTO" in query and "'sending'" in query for query, _ in db.writes)
        assert kwargs["document"][1].startswith(b"PK")
        assert "не найдено" in kwargs["caption"]
        assert "себестоимости: 2" in kwargs["caption"]
        return {"message_id": 42}

    bot.call.side_effect = send
    delivery._run(db, object(), DAY, Event())
    assert db.writes[-1][1][:2] == ("sent", 42)
    assert prepare.call_args.args[2] == {ID: "Venue"}


@pytest.mark.parametrize(
    "error,state,delay",
    [
        (TelegramError(429, retry_after=37), "failed", 37),
        (httpx.ReadTimeout("uncertain"), "unknown", None),
        (TelegramError(403), "unknown", None),
    ],
)
def test_uncertain_and_rate_limit(configured, error, state, delay):
    bot, _ = configured
    bot.call.side_effect = error
    db = Database()
    with pytest.raises(delivery.SyncError):
        delivery._run(db, object(), DAY, Event())
    assert db.writes[-1][1][0] == state
    assert db.writes[-1][1][3] == delay


def test_destination_rechecked(configured, monkeypatch):
    bot, _ = configured
    monkeypatch.setattr(delivery, "destinations", Mock(side_effect=[[TARGET], []]))
    delivery._run(Database(), object(), DAY, Event())
    bot.call.assert_not_called()


def test_changed_bot_does_not_hide_skipped_venues(configured, monkeypatch):
    bot, prepare = configured
    stale = {**TARGET, "department_id": "22222222-2222-4222-8222-222222222222", "bot_id": "456"}
    monkeypatch.setattr(delivery, "destinations", lambda db: [TARGET, stale])
    with pytest.raises(delivery.SyncError, match="low_markup_delivery_incomplete"):
        delivery._run(Database(), object(), DAY, Event())
    assert bot.call.call_count == 1
    assert prepare.call_args.args[2] == {ID: "Venue"}
    with pytest.raises(delivery.SyncError, match="low_markup_delivery_incomplete"):
        delivery._run(
            Database([dict(department_id=ID, state="sent", due=True)]), object(), DAY, Event()
        )
    assert bot.call.call_count == 1


def test_document_upload_preserves_errors():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 7,
                    "chat": {"id": -123},
                    "document": {"file_id": "abc", "file_name": "report.xlsx", "file_size": 4},
                },
            },
        )

    bot = Telegram("123:secret", transport=httpx.MockTransport(handler))
    try:
        assert (
            bot.call(
                "sendDocument", chat_id=-123, document=("report.xlsx", b"xlsx", delivery.MIME)
            )["message_id"]
            == 7
        )
        assert b'name="document"; filename="report.xlsx"' in requests[0].content
        assert b'name="chat_id"' in requests[0].content
    finally:
        bot.close()


def test_backoff_does_not_send(configured):
    bot, prepare = configured
    db = Database([dict(department_id=ID, state="failed", due=False)])
    with pytest.raises(delivery.SyncError):
        delivery._run(db, object(), DAY, Event())
    bot.call.assert_not_called()
    prepare.assert_not_called()


def test_other_venue_continues_after_uncertain_send(configured, monkeypatch):
    bot, prepare = configured
    other_id = "22222222-2222-4222-8222-222222222222"
    other = {**TARGET, "department_id": other_id, "chat_id": -456, "name": "Other"}
    monkeypatch.setattr(delivery, "destinations", lambda db: [TARGET, other])
    prepare.return_value[other_id] = Report("Other", DAY, (), 0)
    bot.call.side_effect = [httpx.ReadTimeout("uncertain"), {"message_id": 9}]
    db = Database()
    with pytest.raises(delivery.SyncError):
        delivery._run(db, object(), DAY, Event())
    assert bot.call.call_count == 2
    assert db.writes[-1][1][:2] == ("sent", 9)


@pytest.mark.parametrize(
    "command,chat,responds",
    [
        ("/chatid", {"id": -123, "type": "group"}, True),
        ("/chatid@REPORT_BOT", {"id": -123, "type": "supergroup"}, True),
        ("/chatid@other_bot", {"id": -123, "type": "group"}, False),
        ("/chatid", {"id": 123, "type": "private"}, False),
        ("/chatid", {"id": -123, "type": "channel"}, False),
    ],
)
def test_chat_id_command_never_exposes_private_previews(command, chat, responds):
    service = SimpleNamespace(settings=SimpleNamespace(bot_username="report_bot"))
    bot = Mock()
    handle_update(service, bot, {"message": {"text": command, "chat": chat}})
    if responds:
        bot.call.assert_called_once_with("sendMessage", chat_id=-123, text="ID этого чата: -123")
    else:
        bot.call.assert_not_called()


def test_scheduler_uses_previous_business_date(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from app import scheduler
    from app.services import sync_jobs

    monkeypatch.setattr(sync_jobs, "ROOT", tmp_path)
    send = Mock()
    monkeypatch.setattr(delivery, "run_reports", send)
    job = next(job for job in scheduler.JOBS if job.key == "low_markup")
    settings, stop = object(), Event()
    slot = job.slot(datetime(2026, 10, 20, 6, 0, tzinfo=UTC))
    assert slot.hour == 9 and slot.day == 20
    scheduler.run_job(job, slot, settings, stop)
    send.assert_called_once_with(settings, DAY, stop)


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"message_id": 7},
        {
            "message_id": True,
            "chat": {"id": -123},
            "document": {"file_id": "a", "file_name": "report.xlsx", "file_size": 4},
        },
        {
            "message_id": 0,
            "chat": {"id": -123},
            "document": {"file_id": "a", "file_name": "report.xlsx", "file_size": 4},
        },
        {
            "message_id": 7,
            "chat": {"id": -124},
            "document": {"file_id": "a", "file_name": "report.xlsx", "file_size": 4},
        },
        {
            "message_id": 7,
            "chat": {"id": -123},
            "document": {"file_id": "a", "file_name": "report.xlsx", "file_size": 5},
        },
        {
            "message_id": 7,
            "chat": {"id": -123},
            "document": {"file_id": "", "file_name": "report.xlsx", "file_size": 4},
        },
    ],
)
def test_document_upload_rejects_unexpected_receipts(receipt):
    bot = Telegram(
        "123:secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": receipt})
        ),
    )
    try:
        with pytest.raises(TelegramError):
            bot.call("sendDocument", chat_id=-123, document=("report.xlsx", b"xlsx", delivery.MIME))
    finally:
        bot.close()


def test_document_upload_accepts_telegram_normalized_filename():
    receipt = {
        "message_id": 7,
        "chat": {"id": -123},
        "document": {"file_id": "abc", "file_name": "normalized.xlsx", "file_size": 4},
    }
    bot = Telegram(
        "123:secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": receipt})
        ),
    )
    try:
        assert (
            bot.call(
                "sendDocument",
                chat_id=-123,
                document=("original—report.xlsx", b"xlsx", delivery.MIME),
            )
            == receipt
        )
    finally:
        bot.close()


def test_wrong_document_size_is_unknown_and_never_automatically_resent(configured, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 7,
                    "chat": {"id": TARGET["chat_id"]},
                    "document": {"file_id": "abc", "file_size": 1},
                },
            },
        )

    bot = Telegram("123:secret", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(delivery, "Telegram", lambda *a, **k: bot)
    db = Database()
    with pytest.raises(delivery.SyncError):
        delivery._run(db, object(), DAY, Event())
    assert db.writes[-1][1][:2] == ("unknown", None)
    with pytest.raises(delivery.SyncError):
        delivery._run(
            Database([dict(department_id=ID, state="unknown", due=True)]), object(), DAY, Event()
        )
    assert len(requests) == 1
