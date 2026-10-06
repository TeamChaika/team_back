"""Synthetic documents and stub Telegram only; native fixture creates its own DB."""

# ruff: noqa: F811
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.documents.telegram import Telegram, handle_update, preview
from app.documents.telegram_cleanup import TelegramError, deliver_cleanup
from tests.test_native_documents import (  # noqa: F401
    PRODUCT,
    SENDER,
    act,
    create,
    database,
    detail,
    service,
)


class Bot:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook
        self.error = None

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if self.error:
            raise self.error
        if method == "sendMessage":
            if self.hook:
                self.hook()
            return {"message_id": len(self.calls)}
        return True


def rows(service):
    with service.database.connection(readonly=True) as db:
        return db.execute(
            "SELECT * FROM native_telegram_messages ORDER BY chat_id,message_id"
        ).fetchall()


def due(service):
    with service.database.connection() as db:
        db.execute("UPDATE native_telegram_messages SET next_attempt_at=now()-interval '1 minute'")


@pytest.mark.parametrize("via_bot", [False, True])
def test_approval_removes_every_recipient_and_pending_duplicate(service, via_bot):
    doc = create(service)
    bot = Bot()
    data = detail(service, doc)
    # A saved original chat stays correct even if profile binding is changed later.
    for chat in [102, 103, 102]:
        preview(bot, service.settings, chat, "waybill", data, service=service)
    assert len(rows(service)) == 3
    if via_bot:
        handle_update(
            service,
            bot,
            {
                "callback_query": {
                    "id": "approval",
                    "from": {"id": 102},
                    "data": f"confirmWaybill:{doc['id']}:{doc['version']}",
                }
            },
        )
    else:
        act(service, doc)
    assert all(row["state"] == "pending" for row in rows(service))
    with service.database.connection() as db:
        db.execute("UPDATE authentication_user SET telegram_id=999 WHERE telegram_id=102")
    while deliver_cleanup(service, bot):
        pass
    assert [row["state"] for row in rows(service)] == ["deleted"] * 3
    assert sorted(p["chat_id"] for method, p in bot.calls if method == "deleteMessage") == [
        102,
        102,
        103,
    ]
    assert detail(service, doc)["submission_state"] == "queued"
    assert not deliver_cleanup(service, bot)


def test_late_send_approval_race_and_multipart_stops(service, monkeypatch):
    doc = create(service)
    monkeypatch.setattr(
        "app.documents.telegram.document_messages", lambda *_: [("page", 1, 2), ("page", 2, 2)]
    )
    bot = Bot(hook=lambda: act(service, doc))
    preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    assert len(bot.calls) == 1
    assert rows(service)[0]["state"] == "pending"
    assert deliver_cleanup(service, bot)


def test_retries_rate_limit_network_and_old_message_fallback(service):
    doc = create(service)
    bot = Bot()
    preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    act(service, doc)
    bot.error = TelegramError(429, "Too Many Requests", 120)
    assert deliver_cleanup(service, bot)
    row = rows(service)[0]
    assert row["state"] == "pending" and row["attempts"] == 1
    assert (row["next_attempt_at"] - row["updated_at"]).total_seconds() == 120
    assert not deliver_cleanup(service, bot)
    due(service)
    bot.error = httpx.ConnectError("synthetic")
    deliver_cleanup(service, bot)
    assert rows(service)[0]["state"] == "pending"
    due(service)
    bot.error = TelegramError(400, "Bad Request: message can't be deleted")
    deliver_cleanup(service, bot)
    assert rows(service)[0]["remove_buttons"]
    bot.error = None
    deliver_cleanup(service, bot)
    assert rows(service)[0]["state"] == "buttons_removed"
    assert bot.calls[-1][0] == "editMessageReplyMarkup"
    assert bot.calls[-1][1]["reply_markup"] == {"inline_keyboard": []}


def test_telegram_retains_safe_provider_error_details():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 42},
            },
        )
    )
    bot = Telegram("synthetic", transport)
    with pytest.raises(TelegramError) as error:
        bot.call("deleteMessage", chat_id=1, message_id=2)
    assert error.value.code == 429 and error.value.retry_after == 42
    assert "synthetic" not in str(error.value)
    bot.close()


def test_pending_command_tracks_duplicates_and_partial_pages(service, monkeypatch):
    doc = create(service)
    bot = Bot()
    update = {
        "message": {"text": "/pending", "chat": {"id": 102, "type": "private"}, "from": {"id": 102}}
    }
    handle_update(service, bot, update)
    handle_update(service, bot, update)
    assert len(rows(service)) == 2
    monkeypatch.setattr(
        "app.documents.telegram.document_messages", lambda *_: [("one", 1, 2), ("two", 2, 2)]
    )
    initial = len(bot.calls)
    original_call = bot.call

    def partial(method, **payload):
        if len(bot.calls) == initial + 1:
            raise httpx.ConnectError("synthetic partial send")
        return original_call(method, **payload)

    bot.call = partial
    with pytest.raises(httpx.ConnectError):
        preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    assert len(rows(service)) == 3
    act(service, doc)
    assert all(row["state"] == "pending" for row in rows(service))


def test_cleanup_not_found_is_idempotent_and_forbidden_is_terminal(service):
    doc = create(service)
    bot = Bot()
    preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    preview(bot, service.settings, 103, "waybill", detail(service, doc), service=service)
    act(service, doc)
    bot.error = TelegramError(400, "Bad Request: message to delete not found")
    deliver_cleanup(service, bot)
    bot.error = TelegramError(403, "Forbidden: bot was blocked by the user")
    deliver_cleanup(service, bot)
    assert [row["state"] for row in rows(service)] == ["deleted", "unavailable"]
    assert not deliver_cleanup(service, bot)


@pytest.mark.parametrize("failures", [1, 3])
def test_record_failure_retries_then_compensates(service, monkeypatch, caplog, failures):
    from app.documents import telegram_cleanup

    doc = create(service)
    bot = Bot()
    original = telegram_cleanup.record
    attempts = []

    def unstable(*args):
        attempts.append(True)
        if len(attempts) <= failures:
            raise RuntimeError("synthetic DB outage")
        return original(*args)

    monkeypatch.setattr(telegram_cleanup, "record", unstable)
    if failures == 1:
        preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
        assert len(rows(service)) == 1 and len(attempts) == 2
        assert len(bot.calls) == 1
    else:
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
        assert not rows(service)
        assert bot.calls[-1][0] == "deleteMessage"
        assert "compensated=True" in caplog.text


def test_record_failure_unavailable_compensation_logs_known_identifiers(
    service, monkeypatch, caplog
):
    doc = create(service)
    bot = Bot()

    def broken_record(*_):
        raise RuntimeError("synthetic DB outage")

    monkeypatch.setattr("app.documents.telegram_cleanup.record", broken_record)
    original_call = bot.call

    def broken_cleanup(method, **payload):
        if method != "sendMessage":
            raise httpx.ConnectError("synthetic transport outage")
        return original_call(method, **payload)

    bot.call = broken_cleanup
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    assert "chat=102 message=1 compensated=False" in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        {"error_code": "429", "description": {}, "parameters": []},
        {"error_code": 429, "description": None, "parameters": {"retry_after": {"bad": True}}},
        {"error_code": 429, "description": False, "parameters": {"retry_after": 10**200}},
    ],
)
def test_malformed_provider_errors_are_safe_and_bounded(response):
    bot = Telegram(
        "synthetic",
        httpx.MockTransport(lambda _: httpx.Response(429, json={"ok": False, **response})),
    )
    with pytest.raises(TelegramError) as error:
        bot.call("deleteMessage", chat_id=1, message_id=2)
    assert type(error.value.code) is int
    assert isinstance(error.value.description, str)
    assert 1 <= error.value.retry_after <= 86400
    bot.close()


def callback(doc, message, version=None):
    version = version or doc["version"]
    data = f"confirmWaybill:{doc['id']}:{version}"
    return {
        "callback_query": {
            "id": str(uuid4()),
            "from": {"id": 102},
            "data": data,
            "message": message,
        }
    }


def test_callback_import_requires_own_keyboard_and_existing_document(service):
    doc = create(service)
    bot = Bot()
    data = f"confirmWaybill:{doc['id']}:{doc['version']}"
    unrelated = {
        "message_id": 900,
        "chat": {"id": 102},
        "reply_markup": {"inline_keyboard": [[{"callback_data": "unrelated"}]]},
    }
    handle_update(service, bot, callback(doc, unrelated))
    assert not rows(service)
    valid = {
        "message_id": 901,
        "chat": {"id": 102},
        "reply_markup": {"inline_keyboard": [[{"callback_data": data}]]},
    }
    # The stale callback cannot reapprove, but its original preview is safely cleaned.
    handle_update(service, bot, callback(doc, valid))
    assert rows(service)[0]["message_id"] == 901 and rows(service)[0]["state"] == "pending"
    missing = {**doc, "id": 999999}
    missing_data = f"confirmWaybill:{missing['id']}:{doc['version']}"
    valid["reply_markup"]["inline_keyboard"][0][0]["callback_data"] = missing_data
    valid["message_id"] = 902
    handle_update(service, bot, callback(missing, valid))
    assert len(rows(service)) == 1


@pytest.mark.parametrize("action", ["confirm_writeoff", "receive", "confirm_receipt"])
def test_every_final_approval_path_queues_cleanup(service, action):
    bot = Bot()
    kind = "writeoff" if action == "confirm_writeoff" else "waybill"
    doc = create(
        service, kind, **({"reason": "Reason", "reason_id": 1} if kind == "writeoff" else {})
    )
    preview(bot, service.settings, 102, kind, detail(service, doc), service=service)
    if action == "confirm_writeoff":
        result = act(service, doc)
    else:
        amount = 1 if action == "confirm_receipt" else 1.25
        result = act(
            service,
            doc,
            "receive",
            payload={
                "request_id": str(uuid4()),
                "version": doc["version"],
                "items": [{"product_id": str(PRODUCT), "amount": amount}],
            },
        )
        if action == "confirm_receipt":
            assert rows(service)[0]["state"] == "active"
            result = act(service, result, "confirm_receipt", SENDER)
    assert result["submission_state"] == "queued"
    assert rows(service)[0]["state"] == "pending"
    assert deliver_cleanup(service, bot)
    assert detail(service, doc)["submission_state"] == "queued"


def test_migration_rerun_imports_legacy_callbacks_and_schedules_existing_active_row(service):
    from psycopg.types.json import Jsonb

    from app.documents.telegram_cleanup import record

    doc = create(service)
    message = {
        "message_id": 777,
        "chat": {"id": 102},
        "reply_markup": {
            "inline_keyboard": [[{"callback_data": f"confirmWaybill:{doc['id']}:{doc['version']}"}]]
        },
    }
    with service.database.connection() as db:
        db.execute(
            "INSERT INTO native_bot_updates(id,data) VALUES (1,%s)",
            (Jsonb(callback(doc, message)),),
        )
    act(service, doc)
    # Simulate an active row imported by a previous migration before watermark backfill.
    record(service, "waybill", doc, 103, 778)
    with service.database.connection() as db:
        db.execute("UPDATE native_telegram_messages SET state='active'")
        db.execute(Path("migrations/documents/0007_telegram_cleanup.sql").read_text())
    assert len(rows(service)) == 2
    assert all(row["state"] == "pending" for row in rows(service))


def test_cleanup_survives_idle_timeout_during_network_request(service, monkeypatch):
    import time
    from contextlib import contextmanager

    doc = create(service)
    bot = Bot()
    preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    act(service, doc)
    connection = service.database.connection

    @contextmanager
    def short_timeout(*, readonly=False):
        with connection(readonly=readonly) as db:
            db.execute("SET LOCAL idle_in_transaction_session_timeout='100ms'")
            yield db

    monkeypatch.setattr(service.database, "connection", short_timeout)
    original_call = bot.call

    def slow_cleanup(method, **payload):
        time.sleep(0.2)
        return original_call(method, **payload)

    bot.call = slow_cleanup
    assert deliver_cleanup(service, bot)
    assert rows(service)[0]["state"] == "deleted"


def test_malformed_rate_limit_does_not_starve_another_message(service):
    doc = create(service)
    bot = Bot()
    for chat in (102, 103):
        preview(bot, service.settings, chat, "waybill", detail(service, doc), service=service)
    act(service, doc)
    bot.error = TelegramError(429, {}, {"not_seconds": True})
    deliver_cleanup(service, bot)
    bot.error = None
    deliver_cleanup(service, bot)
    assert [row["state"] for row in rows(service)] == ["pending", "deleted"]


def test_runtime_role_can_record_approve_and_delete_using_migration_grants(service, monkeypatch):
    from contextlib import contextmanager

    from app.documents.telegram_cleanup import approved, record

    doc = create(service)
    queued = act(service, doc)
    saved = detail(service, queued)
    connection = service.database.connection
    with connection() as db:
        # Simulate existing legacy runtime grants. New-table grants must come from 0007.
        db.execute("GRANT USAGE ON SCHEMA chaika_iiko_documents TO chaika_iiko_app")
        db.execute(
            "GRANT SELECT,UPDATE ON waybills,portal_documents_notification TO chaika_iiko_app"
        )

    @contextmanager
    def runtime_connection(*, readonly=False):
        with connection(readonly=readonly) as db:
            db.execute("SET LOCAL ROLE chaika_iiko_app")
            assert db.execute(
                "SELECT current_user AS name,rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == {"name": "chaika_iiko_app", "rolsuper": False}
            yield db

    monkeypatch.setattr(service.database, "connection", runtime_connection)
    assert record(service, "waybill", doc, 102, 101)
    with service.database.connection() as db:
        approved(db, "waybill", queued)
    bot = Bot()
    assert deliver_cleanup(service, bot)
    assert rows(service)[0]["state"] == "deleted"
    assert not deliver_cleanup(service, bot)
    monkeypatch.setattr(service.database, "connection", connection)
    assert detail(service, queued) == saved


def test_real_concurrent_record_and_approval_never_leaves_active_message(service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.documents.telegram_cleanup import record

    doc = create(service)
    barrier = Barrier(2)

    def register():
        barrier.wait(timeout=5)
        return record(service, "waybill", doc, 102, 999)

    def approve():
        barrier.wait(timeout=5)
        return act(service, doc)

    with ThreadPoolExecutor(2) as pool:
        registered = pool.submit(register)
        approved = pool.submit(approve)
        assert registered.result(timeout=10)
        assert approved.result(timeout=10)["submission_state"] == "queued"
    assert rows(service)[0]["state"] == "pending"
    assert deliver_cleanup(service, Bot())


@pytest.mark.parametrize("iiko_fails", [False, True])
def test_worker_drains_existing_and_callback_approvals_before_iiko(
    service, monkeypatch, iiko_fails
):
    from app.documents import worker

    bot = Bot()
    existing = create(service)
    incoming = create(service)
    for chat in (102, 103, 104):
        preview(bot, service.settings, chat, "waybill", detail(service, existing), service=service)
    for chat in (102, 103):
        preview(bot, service.settings, chat, "waybill", detail(service, incoming), service=service)
    act(service, existing)
    events = []

    def callback_poll(*_):
        states = rows(service)
        assert all(
            row["state"] == "deleted" for row in states if row["document_id"] == existing["id"]
        )
        assert all(
            row["state"] == "active" for row in states if row["document_id"] == incoming["id"]
        )
        events.append("poll approval")
        act(service, incoming)

    def iiko_dispatch(*_):
        events.append("iiko")
        assert all(row["state"] == "deleted" for row in rows(service))
        if iiko_fails:
            raise RuntimeError("synthetic iiko failure")
        return False

    monkeypatch.setattr(worker, "deliver_notification", lambda *_: False)
    monkeypatch.setattr(worker, "poll", callback_poll)
    monkeypatch.setattr(worker, "deliver_one", iiko_dispatch)
    with worker.worker_leader(service.database) as leader:
        if iiko_fails:
            with pytest.raises(RuntimeError, match="synthetic iiko failure"):
                worker.process_jobs(service, bot, leader)
        else:
            assert worker.process_jobs(service, bot, leader)
    assert events == ["poll approval", "iiko"]
    assert sum(method == "deleteMessage" for method, _ in bot.calls) == 5


def test_cleanup_batch_is_bounded_by_count_and_elapsed_time(monkeypatch):
    from app.documents import worker

    calls = []
    monkeypatch.setattr(worker, "deliver_cleanup", lambda *_: calls.append(True) or True)
    monkeypatch.setattr(worker, "monotonic", lambda: 0)
    worker.drain_cleanup(None, None)
    assert len(calls) == 20
    calls.clear()
    times = iter([0, 0, 3])
    monkeypatch.setattr(worker, "monotonic", lambda: next(times))
    worker.drain_cleanup(None, None)
    assert len(calls) == 1


def test_rate_limit_defers_every_pending_preview_in_same_chat(service):
    doc = create(service)
    bot = Bot()
    for _ in range(3):
        preview(bot, service.settings, 102, "waybill", detail(service, doc), service=service)
    act(service, doc)
    bot.error = TelegramError(429, "Too Many Requests", 120)
    assert deliver_cleanup(service, bot)
    assert not deliver_cleanup(service, bot)
    assert sum(method == "deleteMessage" for method, _ in bot.calls) == 1
    assert all(row["state"] == "pending" for row in rows(service))
