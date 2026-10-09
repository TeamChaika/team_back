"""Bot replacement isolation, using disposable PostgreSQL and synthetic Telegram only."""

# ruff: noqa: F811, F401
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from app.documents import password_recovery, telegram_link, worker
from app.documents.telegram_cleanup import approved, deliver_cleanup, record
from app.documents.telegram_identity import bot_namespace
from tests.test_native_documents import SENDER, create, detail, service
from tests.test_native_documents import database as native_database
from tests.test_telegram_cleanup import Bot
from tests.test_tenant_documents import companies
from tests.test_tenant_migrations_postgres import empty_database


@pytest.fixture(scope="module")
def database():
    source = native_database.__wrapped__()
    database = next(source)
    with database.connection() as db:
        db.execute(Path("migrations/documents/0002_telegram_link.sql").read_text())
        db.execute(Path("migrations/documents/0003_password_recovery.sql").read_text())
        db.execute(
            Path("migrations/tenant/20261010110000_tenant_telegram_bot_namespace.sql")
            .read_text()
            .replace("{documents}", "chaika_iiko_documents")
        )
    yield database
    try:
        next(source)
    except StopIteration:
        pass


@pytest.fixture
def tenant(service, monkeypatch):
    monkeypatch.setattr(
        "app.documents.telegram_identity.runtime_of", lambda *_: SimpleNamespace(mode="tenant")
    )
    service.settings.bot_token = SecretStr("123:first-secret")
    service.settings.bot_username = "synthetic_bot"
    service.settings.native_enabled = True
    return service


class PollBot:
    def __init__(self, updates):
        self.updates, self.offsets = updates, []

    def call(self, method, **payload):
        assert method == "getUpdates"
        self.offsets.append(payload["offset"])
        result, self.updates = self.updates, []
        return result


def test_new_bot_does_not_reuse_cursor_or_update_id(tenant, monkeypatch):
    received = []
    monkeypatch.setattr(
        worker, "handle_update", lambda service, bot, update: received.append(update)
    )
    with tenant.database.connection() as db:
        db.execute(
            "INSERT INTO native_jobs(name,data) VALUES ('telegram-offset',%s)",
            (Jsonb({"offset": 9000}),),
        )
        db.execute(
            "INSERT INTO native_bot_updates(id,data) VALUES (1,%s)",
            (Jsonb({"update_id": 1, "legacy": True}),),
        )
    first = PollBot([{"update_id": 1, "first": True}])
    worker.poll(tenant, first)
    tenant.settings.bot_token = SecretStr("123:rotated-secret")
    worker.poll(tenant, first)
    assert first.offsets == [0, 2]
    tenant.settings.bot_token = SecretStr("456:other-bot")
    second = PollBot([{"update_id": 1, "second": True}])
    worker.poll(tenant, second)
    assert second.offsets == [0]
    assert received == [{"update_id": 1, "first": True}, {"update_id": 1, "second": True}]
    with tenant.database.connection() as db:
        states = db.execute(
            "SELECT bot_id,state FROM native_bot_updates ORDER BY bot_id"
        ).fetchall()
    assert [(row["bot_id"], row["state"]) for row in states] == [
        ("", "pending"),
        ("123", "done"),
        ("456", "done"),
    ]


def test_cleanup_ignores_old_and_unknown_bot_even_with_same_message_id(tenant):
    doc = detail(tenant, create(tenant))
    record(tenant, "waybill", doc, 102, 7)
    tenant.settings.bot_token = SecretStr("456:other-bot")
    record(tenant, "waybill", doc, 102, 7)
    with tenant.database.connection() as db:
        db.execute(
            "INSERT INTO native_telegram_messages(chat_id,message_id,kind,document_id,version) "
            "VALUES (102,8,'waybill',%s,%s)",
            (doc["id"], doc["version"]),
        )
        approved(db, "waybill", doc)
    bot = Bot()
    assert deliver_cleanup(tenant, bot)
    assert not deliver_cleanup(tenant, bot)
    assert bot.calls == [("deleteMessage", {"chat_id": 102, "message_id": 7})]
    with tenant.database.connection() as db:
        rows = db.execute(
            "SELECT bot_id,state FROM native_telegram_messages ORDER BY bot_id"
        ).fetchall()
    assert [(r["bot_id"], r["state"]) for r in rows] == [
        ("", "pending"),
        ("123", "pending"),
        ("456", "deleted"),
    ]
    tenant.settings.bot_token = SecretStr("123:rotated-secret")
    assert deliver_cleanup(tenant, bot)
    assert not deliver_cleanup(tenant, bot)


def test_links_bound_to_bot_but_survive_token_rotation(tenant):
    with tenant.database.connection() as db:
        db.execute("UPDATE authentication_user SET telegram_id=NULL WHERE id=1")
    raw = telegram_link.issue(tenant, SENDER)["url"].split("link_")[1]
    tenant.settings.bot_token = SecretStr("456:other-bot")
    with pytest.raises(HTTPException):
        telegram_link.consume(tenant, raw, 101)
    tenant.settings.bot_token = SecretStr("123:rotated-secret")
    assert telegram_link.consume(tenant, raw, 101) == {"linked": True}


def test_recovery_bound_to_bot_but_survives_token_rotation(tenant):
    raw = password_recovery.issue(tenant, 101).split("token=")[1]
    tenant.settings.bot_token = SecretStr("456:other-bot")
    with pytest.raises(HTTPException):
        password_recovery.claim(tenant, raw)
    tenant.settings.bot_token = SecretStr("123:rotated-secret")
    assert password_recovery.claim(tenant, raw)["telegram_id"] == 101


def test_invalid_tenant_bot_identity_fails_closed(tenant):
    tenant.settings.bot_token = SecretStr("invalid")
    with pytest.raises(ValueError, match="identity"):
        bot_namespace(tenant)


def test_restricted_real_tenant_database_namespaces(companies, monkeypatch):
    received = []
    monkeypatch.setattr(worker, "handle_update", lambda *args: received.append(args[2]))
    service_a, service_b = companies[0][0], companies[1][0]
    service_a.settings.bot_token = SecretStr("123:synthetic")
    service_b.settings.bot_token = SecretStr("456:synthetic")
    for company_service in (service_a, service_b):
        worker.poll(company_service, PollBot([{"update_id": 1}]))
        with company_service.database.connection() as db:
            db.execute(
                "INSERT INTO native_telegram_messages(bot_id,chat_id,message_id,kind,"
                "document_id,version,state) VALUES (%s,102,7,'waybill',1,1,'pending')",
                (bot_namespace(company_service),),
            )
    service_a.settings.bot_token = SecretStr("456:replacement")
    bot = Bot()
    assert not deliver_cleanup(service_a, bot)
    assert deliver_cleanup(service_b, bot)
    assert bot.calls == [("deleteMessage", {"chat_id": 102, "message_id": 7})]
    new = PollBot([{"update_id": 1}])
    worker.poll(service_a, new)
    assert new.offsets == [0]
    assert len(received) == 3
