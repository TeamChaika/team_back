"""Ownership and transaction regression tests; Telegram is always a local fake."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.documents import telegram_link as links
from app.documents.telegram import handle_update
from app.documents.worker import poll
from tests.test_native_documents import RECEIVER, SENDER
from tests.test_native_documents import service as native_service


@pytest.fixture(scope="module")
def database():
    import os

    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from app.documents.database import DocumentDatabase

    url = os.environ.get("CHAIKA_DOCUMENTS_TEST_DSN") or os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL required")
    if conninfo_to_dict(url).get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("Only loopback disposable test databases are allowed")
    with psycopg.connect(url, autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='chaika_iiko_app'").fetchone():
            db.execute("CREATE ROLE chaika_iiko_app")
        if not db.execute(
            "SELECT 1 FROM pg_database WHERE datname='telegram_profile_tests'"
        ).fetchone():
            db.execute("CREATE DATABASE telegram_profile_tests")
    test_url = make_conninfo(url, dbname="telegram_profile_tests")
    with psycopg.connect(test_url, autocommit=True) as db:
        db.execute("DROP SCHEMA IF EXISTS chaika_iiko_documents CASCADE")
        db.execute(Path("tests/fixtures/documents_schema.sql").read_text())
        db.execute(Path("migrations/documents/0001_native_runtime.sql").read_text())
        db.execute(Path("migrations/documents/0004_receipt_discrepancies.sql").read_text())
        db.execute(Path("migrations/documents/0002_telegram_link.sql").read_text())
    database = DocumentDatabase(test_url)
    yield database
    database.close()


@pytest.fixture
def service(database):
    return native_service.__wrapped__(database)


@pytest.fixture
def ready(service):
    with service.database.connection() as db:
        db.execute("UPDATE authentication_user SET telegram_id=NULL WHERE id IN (1,2)")
    service.settings.native_enabled = True
    service.settings.bot_token = SecretStr("fake-test-token")
    service.settings.bot_username = "test_chaika_bot"
    return service


def token(service, portal_id=SENDER):
    result = links.issue(service, portal_id)
    raw = result["url"].split("link_")[1]
    assert len("link_" + raw) <= 64
    return raw


def rejected(call, status=None):
    with pytest.raises(HTTPException) as exc:
        call()
    if status:
        assert exc.value.status_code == status


def test_roundtrip_preserves_grants_and_history(ready):
    with ready.database.connection() as db:
        grants = db.execute("SELECT * FROM portal_documents_grant ORDER BY id").fetchall()
        events = db.execute("SELECT * FROM portal_documents_accessevent ORDER BY id").fetchall()
    raw = token(ready)
    with ready.database.connection() as db:
        saved = db.execute("SELECT * FROM native_telegram_link").fetchone()
        assert raw not in str(saved)
    assert links.consume(ready, raw, 9001) == {"linked": True}
    assert links.status(ready, SENDER)["telegram_id"] == "9001"
    rejected(lambda: links.consume(ready, raw, 9002), 400)
    rejected(lambda: token(ready), 409)
    assert links.unlink(ready, SENDER) == {"linked": False}
    assert links.status(ready, SENDER)["linked"] is False
    with ready.database.connection() as db:
        assert grants == db.execute("SELECT * FROM portal_documents_grant ORDER BY id").fetchall()
        assert (
            events
            == db.execute("SELECT * FROM portal_documents_accessevent ORDER BY id").fetchall()
        )


def test_expiry_reissue_and_unlink_invalidate_pending(ready):
    raw = token(ready)
    with ready.database.connection() as db:
        db.execute("UPDATE native_telegram_link SET expires_at=now()-interval '1 second'")
    rejected(lambda: links.consume(ready, raw, 9001), 400)
    old, current = token(ready), token(ready)
    rejected(lambda: links.consume(ready, old, 9001), 400)
    links.unlink(ready, SENDER)
    rejected(lambda: links.consume(ready, current, 9001), 400)


@pytest.mark.parametrize("disabled", ["portal", "document"])
def test_disabled_identity_at_issue_and_consumption(ready, disabled):
    raw = token(ready)
    with ready.database.connection() as db:
        if disabled == "portal":
            db.execute("UPDATE portal_access SET active=false WHERE id=%s", (SENDER,))
        else:
            db.execute("UPDATE authentication_user SET is_active=false WHERE id=1")
    rejected(lambda: token(ready), 403)
    rejected(lambda: links.consume(ready, raw, 9001), 403)


def test_empty_profile_creation_has_no_grants(ready):
    new = uuid4()
    with ready.database.connection() as db:
        db.execute("INSERT INTO portal_access VALUES (%s,true,'[]',false)", (new,))
    assert links.status(ready, new)["linked"] is False
    raw = token(ready, new)
    links.consume(ready, raw, 9999)
    with ready.database.connection() as db:
        row = db.execute(
            "SELECT u.* FROM authentication_user u JOIN portal_documents_userlink l "
            "ON l.user_id=u.id WHERE l.supabase_id=%s",
            (new,),
        ).fetchone()
        assert not row["is_staff"] and not row["is_superuser"]
        assert not db.execute(
            "SELECT 1 FROM portal_documents_grant WHERE user_id=%s", (row["id"],)
        ).fetchone()


@pytest.mark.parametrize("same_token", [True, False])
def test_parallel_consumption_has_one_winner(ready, same_token):
    first = token(ready)
    second = first if same_token else token(ready, RECEIVER)
    barrier = Barrier(2)

    def consume(raw):
        barrier.wait(timeout=5)
        try:
            links.consume(ready, raw, 9999)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, (first, second)))
    assert sorted(results) == [False, True]
    with ready.database.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) n FROM authentication_user WHERE telegram_id=9999"
            ).fetchone()["n"]
            == 1
        )


def test_existing_other_link_and_admin_revision_cannot_be_overwritten(ready):
    raw = token(ready)
    rejected(lambda: links.consume(ready, raw, 103), 409)
    with ready.database.connection() as db:
        db.execute("UPDATE portal_documents_userlink SET revision=revision+1 WHERE user_id=1")
    rejected(lambda: links.consume(ready, raw, 9999), 400)


class Bot:
    def __init__(self, updates=None):
        self.updates = updates or []
        self.calls = []

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return self.updates if method == "getUpdates" else {"message_id": 1}


def update(raw, chat_type="private"):
    return {
        "update_id": 333,
        "message": {
            "text": f"/start link_{raw}",
            "chat": {"id": 9999, "type": chat_type},
            "from": {"id": 9999},
        },
    }


def test_bot_private_only_and_queue_contains_no_raw_token(ready):
    raw = token(ready)
    bot = Bot()
    handle_update(ready, bot, update(raw, "group"))
    assert not bot.calls
    assert not links.status(ready, SENDER)["linked"]
    bot = Bot([update(raw)])
    poll(ready, bot)
    assert links.status(ready, SENDER)["linked"]
    assert any("подключён" in call[1].get("text", "") for call in bot.calls)
    with ready.database.connection() as db:
        data = db.execute("SELECT data FROM native_bot_updates WHERE id=333").fetchone()["data"]
        assert raw not in str(data)
    handle_update(
        ready,
        bot,
        {
            "message": {
                "text": "/start",
                "chat": {"id": 9999, "type": "private"},
                "from": {"id": 9999},
            }
        },
    )
    assert "Ваш Telegram ID" in bot.calls[-1][1]["text"]


def test_unlink_racing_consumption_leaves_no_link_or_token(ready):
    raw = token(ready)
    barrier = Barrier(2)

    def consume():
        barrier.wait(timeout=5)
        try:
            links.consume(ready, raw, 9999)
        except HTTPException:
            pass

    def unlink():
        barrier.wait(timeout=5)
        links.unlink(ready, SENDER)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(consume), executor.submit(unlink)]
        for result in results:
            result.result(timeout=10)
    assert links.status(ready, SENDER)["linked"] is False
    rejected(lambda: links.consume(ready, raw, 9999), 400)


def test_username_resolution_caches_and_masks_failure(ready, monkeypatch):
    from app.documents.telegram import Telegram

    ready.settings.bot_username = ""
    calls = []

    def call(self, method, **kwargs):
        calls.append(method)
        return {"username": "resolved_test_bot"}

    monkeypatch.setattr(Telegram, "call", call)
    assert "t.me/resolved_test_bot?" in links.issue(ready, SENDER)["url"]
    links.issue(ready, SENDER)
    assert calls == ["getMe"]
    del ready._telegram_username_cache

    def broken(self, method, **kwargs):
        raise RuntimeError("Sensitive URL/token must not be returned")

    monkeypatch.setattr(Telegram, "call", broken)
    with pytest.raises(HTTPException) as exc:
        links.issue(ready, SENDER)
    assert exc.value.status_code == 503
    assert "Sensitive" not in exc.value.detail


def test_runtime_role_can_issue_consume_unlink_and_create_empty_profile(ready, monkeypatch):
    """Exercise the real restricted role; new-table privileges come only from migration."""
    from contextlib import contextmanager

    database = ready.database
    original_connection = database.connection
    new = uuid4()
    with original_connection() as db:
        db.execute("GRANT USAGE ON SCHEMA chaika_iiko_documents TO chaika_iiko_app")
        db.execute("GRANT SELECT ON portal_access TO chaika_iiko_app")
        db.execute(
            "GRANT SELECT,INSERT,UPDATE ON authentication_user,portal_documents_userlink "
            "TO chaika_iiko_app"
        )
        db.execute(
            "GRANT USAGE ON SEQUENCE authentication_user_id_seq,portal_documents_userlink_id_seq "
            "TO chaika_iiko_app"
        )
        db.execute("INSERT INTO portal_access VALUES (%s,true,'[]',false)", (new,))

    @contextmanager
    def restricted_connection(*, readonly=False):
        with original_connection(readonly=readonly) as db:
            db.execute("SET LOCAL ROLE chaika_iiko_app")
            identity = db.execute(
                "SELECT current_user AS name,rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert identity == {"name": "chaika_iiko_app", "rolsuper": False}
            yield db

    monkeypatch.setattr(database, "connection", restricted_connection)
    for portal_id, telegram_id in ((SENDER, 9997), (new, 9998)):
        raw = token(ready, portal_id)
        links.consume(ready, raw, telegram_id)
        assert links.status(ready, portal_id)["telegram_id"] == str(telegram_id)
        links.unlink(ready, portal_id)
        assert links.status(ready, portal_id)["linked"] is False


@pytest.mark.parametrize("local_address", ["", "::", "0.0.0.0"])
def test_username_resolution_honors_telegram_local_address(monkeypatch, local_address):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.documents.config import DocumentSettings

    settings = DocumentSettings(
        _env_file=None,
        bot_token="fake-test-token",
        bot_username="",
        telegram_local_address=local_address,
    )
    bot_factory = Mock()
    bot_factory.return_value.call.return_value = {"username": "resolved_test_bot"}
    transport_factory = Mock()
    monkeypatch.setattr("app.documents.telegram.Telegram", bot_factory)
    monkeypatch.setattr(links.httpx, "HTTPTransport", transport_factory)

    assert links._username(SimpleNamespace(settings=settings)) == "resolved_test_bot"
    if local_address:
        transport_factory.assert_called_once_with(local_address=local_address)
        bot_factory.assert_called_once_with(
            "fake-test-token", transport=transport_factory.return_value
        )
    else:
        transport_factory.assert_not_called()
        bot_factory.assert_called_once_with("fake-test-token", transport=None)
    bot_factory.return_value.call.assert_called_once_with("getMe")
    bot_factory.return_value.close.assert_called_once_with()
