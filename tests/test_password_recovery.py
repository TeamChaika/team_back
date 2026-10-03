"""No external Telegram/Auth; disposable loopback DB checks real atomic consumption."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.documents import password_recovery as recovery
from app.documents.telegram import handle_update
from app.documents.telegram_link import queued_update
from app.portal import create_portal
from app.web.password_recovery import ResetPassword, reset
from app.web.settings import WebSettings
from tests.test_portal import FakeRepository
from tests.test_telegram_link import SENDER, database, ready, service  # noqa: F401, F811


@pytest.fixture
def linked(ready):  # noqa: F811
    with ready.database.connection() as db:
        db.execute(Path("migrations/documents/0003_password_recovery.sql").read_text())
    with ready.database.connection() as db:
        db.execute("DELETE FROM native_password_recovery")
        db.execute("UPDATE authentication_user SET telegram_id=9001 WHERE id=1")
    return ready


def raw(native):
    return recovery.issue(native, 9001).split("#token=")[1]


def web():
    return WebSettings(_env_file=None, auth_admin_key="fake-admin", anon_key="fake")


def test_only_unique_existing_active_binding(linked):
    with pytest.raises(HTTPException):
        recovery.issue(linked, 9002)
    with linked.database.connection() as db:
        db.execute("UPDATE authentication_user SET is_active=false WHERE id=1")
    with pytest.raises(HTTPException):
        recovery.issue(linked, 9001)
    with linked.database.connection() as db:
        db.execute("UPDATE authentication_user SET is_active=true WHERE id=1")
        db.execute("UPDATE portal_access SET active=false WHERE id=%s", (SENDER,))
    with pytest.raises(HTTPException):
        recovery.issue(linked, 9001)


def test_hash_only_expiration_and_single_use(linked):
    token = raw(linked)
    with linked.database.connection() as db:
        row = db.execute("SELECT * FROM native_password_recovery").fetchone()
        assert token not in str(row)
    ticket = recovery.claim(linked, token)
    assert ticket["portal_id"] == SENDER
    with pytest.raises(HTTPException):
        recovery.claim(linked, token)
    with pytest.raises(HTTPException):
        recovery.issue(linked, 9001)
    with linked.database.connection() as db:
        db.execute(
            "UPDATE native_password_recovery "
            "SET expires_at=now()-interval '1 second', claimed_at=NULL"
        )
    with pytest.raises(HTTPException):
        recovery.claim(linked, token)


def test_concurrent_claim_has_one_winner(linked):
    token = raw(linked)

    def claim(_):
        try:
            recovery.claim(linked, token)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(claim, range(2))) == [False, True]


def test_unlink_or_relink_invalidates_outstanding_token(linked):
    token = raw(linked)
    with linked.database.connection() as db:
        db.execute(
            "UPDATE portal_documents_userlink SET revision=revision+1 WHERE supabase_id=%s",
            (SENDER,),
        )
    upstream = Mock()
    with pytest.raises(HTTPException):
        reset(
            linked,
            web(),
            Mock(),
            ResetPassword(token=token, new_password="new-pass-123"),
            transport=httpx.MockTransport(upstream),
        )
    upstream.assert_not_called()


def test_confirmed_update_only_target_password_and_completion(linked):
    token = raw(linked)
    repo = Mock()

    def provider(request):
        assert request.method == "PUT"
        assert request.url.path == "/auth/v1/admin/users/" + str(SENDER)
        assert request.headers["authorization"] == "Bearer fake-admin"
        assert json.loads(request.content) == {"password": "new-pass-123"}
        return httpx.Response(200, json={"id": str(SENDER)})

    assert reset(
        linked,
        web(),
        repo,
        ResetPassword(token=token, new_password="new-pass-123"),
        transport=httpx.MockTransport(provider),
    ) == {"status": "ok"}
    repo.complete_password_change.assert_called_once_with(SENDER)
    with pytest.raises(HTTPException):
        recovery.claim(linked, token)


@pytest.mark.parametrize("mode", ["timeout", "500", "wrong-user", "invalid-json"])
def test_unknown_update_never_retries_or_clears_gate(linked, mode):
    token, repo, calls = raw(linked), Mock(), []

    def provider(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("private password must never be logged")
        if mode == "500":
            return httpx.Response(500)
        if mode == "invalid-json":
            return httpx.Response(200, content="not-json")
        return httpx.Response(200, json={"id": "different"})

    with pytest.raises(HTTPException) as error:
        reset(
            linked,
            web(),
            repo,
            ResetPassword(token=token, new_password="new-pass-123"),
            transport=httpx.MockTransport(provider),
        )
    assert error.value.status_code == 503
    assert len(calls) == 1
    repo.complete_password_change.assert_not_called()
    with pytest.raises(HTTPException):
        recovery.claim(linked, token)


def test_http_origin_validation_no_session_required_and_cookie_clear(linked, monkeypatch):
    monkeypatch.setattr(linked, "close", lambda: None)
    token = raw(linked)
    repo = FakeRepository()
    repo.complete_password_change = Mock()
    app = create_portal(
        Settings(),
        web(),
        repository=repo,
        document_service=linked,
        auth_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": str(SENDER)})
        ),
    )
    payload = {"token": token, "new_password": "new-pass-123"}
    with TestClient(app) as client:
        assert client.get("/api/auth/recovery/telegram").json()["url"].endswith("?start=recover")
        assert client.post("/api/auth/recovery/reset", json=payload).status_code == 403
        headers = {"Origin": web().origin}
        invalid = client.post(
            "/api/auth/recovery/reset", headers=headers, json={**payload, "user_id": str(SENDER)}
        )
        assert invalid.status_code == 422 and token not in invalid.text
        response = client.post("/api/auth/recovery/reset", headers=headers, json=payload)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert (
            client.post("/api/auth/recovery/reset", headers=headers, json=payload).status_code
            == 400
        )


def test_bot_private_numeric_sender_confirmation_only(linked):
    bot = Mock()
    update = {
        "message": {
            "text": "/start recover",
            "chat": {"type": "group", "id": 9001},
            "from": {"id": 9001},
        }
    }
    handle_update(linked, bot, update)
    bot.call.assert_not_called()
    update["message"]["chat"]["type"] = "private"
    handle_update(linked, bot, update)
    assert (
        bot.call.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == "recovery_confirm"
    )
    bot.reset_mock()
    callback = {
        "callback_query": {
            "id": "synthetic",
            "data": "recovery_confirm",
            "message": update["message"],
            "from": {"id": 9002},
        }
    }
    handle_update(linked, bot, callback)
    bot.call.assert_not_called()
    callback["callback_query"]["from"]["id"] = 9001
    handle_update(linked, bot, callback)
    sent = bot.call.call_args_list[0].kwargs
    assert sent["chat_id"] == 9001
    assert "/reset-password#token=" in sent["reply_markup"]["inline_keyboard"][0][0]["url"]


def test_quoted_or_forwarded_recovery_credential_not_persisted():
    token = "a" * 43
    update = {
        "message": {
            "text": "help",
            "reply_to_message": {
                "reply_markup": {
                    "inline_keyboard": [
                        [{"url": "https://dashboard.chaika.team/reset-password#token=" + token}]
                    ]
                }
            },
        }
    }
    assert token not in json.dumps(queued_update(update))


def test_ambiguous_legacy_bindings_fail_closed(linked):
    with pytest.raises(HTTPException):
        with linked.database.connection() as db:
            db.execute(
                "ALTER TABLE authentication_user "
                "DROP CONSTRAINT authentication_user_telegram_id_key"
            )
            db.execute("UPDATE authentication_user SET telegram_id=9001 WHERE id=2")
            recovery.identity(db, 9001)


def test_lost_completion_keeps_token_spent(linked):
    import psycopg

    token, repo = raw(linked), Mock()
    repo.complete_password_change.side_effect = psycopg.OperationalError("synthetic")
    with pytest.raises(HTTPException) as error:
        reset(
            linked,
            web(),
            repo,
            ResetPassword(token=token, new_password="new-pass-123"),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": str(SENDER)})
            ),
        )
    assert error.value.status_code == 503
    assert "Новый пароль сохранён" in error.value.detail
    with pytest.raises(HTTPException):
        recovery.claim(linked, token)


@pytest.mark.parametrize("length", [7, 129])
def test_recovery_password_length_validation(length):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResetPassword(token="a" * 43, new_password="x" * length)


def test_portal_rate_limit(linked, monkeypatch):
    monkeypatch.setattr(linked, "close", lambda: None)
    app = create_portal(
        Settings(),
        web(),
        repository=FakeRepository(),
        document_service=linked,
        auth_transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        for _ in range(10):
            assert client.get("/api/auth/recovery/telegram").status_code == 200
        assert client.get("/api/auth/recovery/telegram").status_code == 429
