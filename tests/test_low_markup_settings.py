"""Destination validation and administrator saves; Telegram is always mocked."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import SecretStr, ValidationError

from app.web import low_markup_reports as api
from tests.test_low_markup_postgres import ACTOR, VENUE
from tests.test_low_markup_postgres import database as database


class Administration:
    def __init__(self, db):
        self.db = db
        self.repo = self
        self.audit = Mock()

    @contextmanager
    def connection(self, **kwargs):
        yield self.db

    @contextmanager
    def write(self, actor):
        if actor != ACTOR:
            raise HTTPException(403, "Forbidden")
        with self.db.transaction():
            yield self.db


@pytest.mark.parametrize("chat", ["123", "0", "-0", "@group", "-9223372036854775808", "-123.1"])
def test_invalid_chat_id(chat):
    with pytest.raises(ValidationError):
        api.DestinationWrite(revision=0, chat_id=chat, enabled=True)


@pytest.mark.parametrize(
    "chat,member",
    [
        ({"id": -123, "type": "channel", "title": "Group"}, {"status": "administrator"}),
        (
            {"id": -123, "type": "channel", "title": "Channel"},
            {"status": "administrator", "can_post_messages": False},
        ),
        (
            {"id": -123, "type": "channel", "title": "Channel"},
            {"status": "member", "can_post_messages": True},
        ),
        ({"id": -124, "type": "supergroup", "title": "Group"}, {"status": "member"}),
        ({"id": -123, "type": "supergroup", "title": "Group"}, {"status": "left"}),
        (
            {
                "id": -123,
                "type": "supergroup",
                "title": "Group",
                "permissions": {"can_send_documents": False},
            },
            {"status": "member"},
        ),
        (
            {"id": -123, "type": "supergroup", "title": "Group"},
            {"status": "restricted", "is_member": True, "can_send_documents": False},
        ),
    ],
)
def test_unusable_group_rejected(monkeypatch, chat, member):
    bot = Mock()
    bot.call.side_effect = [chat, member]
    monkeypatch.setattr(api, "Telegram", lambda *a, **k: bot)
    with pytest.raises(HTTPException) as error:
        api._verified_chat(
            SimpleNamespace(bot_token=SecretStr("123:secret"), telegram_local_address=""), "-123"
        )
    assert error.value.status_code == 422
    bot.close.assert_called_once()


def test_save_revision_and_disable_without_bot(database, monkeypatch):
    db, _ = database
    administration = Administration(db)
    current = db.execute("SELECT revision FROM chaika.low_markup_destinations").fetchone()[
        "revision"
    ]
    verifier = Mock(return_value=("123", "Verified group"))
    monkeypatch.setattr(api, "_verified_chat", verifier)
    monkeypatch.setattr(
        api, "DocumentSettings", lambda: SimpleNamespace(bot_token=SecretStr(""), bot_username="")
    )
    result = api.save_destination(
        administration,
        ACTOR,
        VENUE,
        api.DestinationWrite(revision=current, chat_id="-123", enabled=False),
    )
    verifier.assert_not_called()
    assert result["departments"][0]["enabled"] is False
    assert result["departments"][0]["revision"] == current + 1
    with pytest.raises(HTTPException) as error:
        api.save_destination(
            administration,
            ACTOR,
            VENUE,
            api.DestinationWrite(revision=current, chat_id="-123", enabled=False),
        )
    assert error.value.status_code == 409
    assert administration.audit.call_count == 1


def test_scope_and_unknown_venue_before_telegram(database, monkeypatch):
    db, _ = database
    verifier = Mock(side_effect=AssertionError("No Telegram before authorization"))
    monkeypatch.setattr(api, "_verified_chat", verifier)
    db.execute(
        "INSERT INTO chaika.operation_settings VALUES(true,1,%s,now(),%s)",
        (Jsonb({"sync": {"mode": "selected", "department_ids": []}}), ACTOR),
    )
    revision = db.execute("SELECT revision FROM chaika.low_markup_destinations").fetchone()[
        "revision"
    ]
    for actor, venue, status, rev in (
        (ACTOR, VENUE, 422, revision),
        (ACTOR, UUID(int=99), 404, 0),
        (UUID(int=99), VENUE, 403, revision),
    ):
        with pytest.raises(HTTPException) as error:
            api.save_destination(
                Administration(db),
                actor,
                venue,
                api.DestinationWrite(revision=rev, chat_id="-123", enabled=True),
            )
        assert error.value.status_code == status
    verifier.assert_not_called()


@pytest.mark.parametrize(
    "kind,member",
    [
        ("channel", {"status": "administrator", "can_post_messages": True}),
        ("group", {"status": "member"}),
        ("supergroup", {"status": "administrator"}),
        ("supergroup", {"status": "restricted", "is_member": True, "can_send_documents": True}),
    ],
)
def test_usable_channel_or_group_accepted(monkeypatch, kind, member):
    bot = Mock()
    bot.call.side_effect = [{"id": -123, "type": kind, "title": "Verified"}, member]
    monkeypatch.setattr(api, "Telegram", lambda *a, **k: bot)
    assert api._verified_chat(
        SimpleNamespace(bot_token=SecretStr("123:secret"), telegram_local_address=""), "-123"
    ) == ("123", "Verified")
    bot.close.assert_called_once()
