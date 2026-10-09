"""Tenant recovery calls the private verifier; no Auth/Telegram requests are made."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.documents import telegram_link
from app.documents.service import DocumentService
from app.saas_admin.repository import Problem
from app.web.password_recovery import create_recovery_router
from tests.test_tenant_migrations import runtime_for


@pytest.fixture
def recovery_app():
    runtime = runtime_for()
    native = DocumentService.__new__(DocumentService)
    native.database = SimpleNamespace(runtime=runtime)
    native.settings = SimpleNamespace(
        native_enabled=True,
        worker_enabled=True,
        bot_token=SecretStr("synthetic-own-token"),
        bot_username="own_company_bot",
    )
    verifier = Mock()
    verifier.recover_company_password.return_value = {"ok": True, "completed": True}
    app = FastAPI()
    app.state.documents = native
    app.state.saas_auth_repository = verifier

    def origin(request):
        if request.headers.get("origin") != runtime.frontend_origin:
            raise HTTPException(403, "Origin denied")

    app.state.auth = SimpleNamespace(check_origin=origin)
    app.include_router(
        create_recovery_router(
            SimpleNamespace(runtime=runtime), SimpleNamespace(auth_admin_key=SecretStr(""))
        )
    )
    return runtime, native, verifier, TestClient(app)


def test_tenant_start_requires_own_explicit_bot_worker_and_origin(recovery_app):
    runtime, native, verifier, client = recovery_app
    assert client.get("/api/auth/recovery/telegram").status_code == 403
    headers = {"origin": runtime.frontend_origin}
    response = client.get("/api/auth/recovery/telegram", headers=headers)
    assert response.json() == {"url": "https://t.me/own_company_bot?start=recover"}
    for field, value in (
        ("worker_enabled", False),
        ("bot_username", ""),
        ("bot_token", SecretStr("")),
    ):
        previous = getattr(native.settings, field)
        setattr(native.settings, field, value)
        assert not telegram_link._available(native)
        assert client.get("/api/auth/recovery/telegram", headers=headers).status_code == 503
        setattr(native.settings, field, previous)
    verifier.recover_company_password.assert_not_called()


def test_reset_delegates_only_capability_and_password_not_user_or_admin_key(recovery_app):
    runtime, native, verifier, client = recovery_app
    body = {"token": "a" * 43, "new_password": "synthetic-new-password"}
    assert client.post("/api/auth/recovery/reset", json=body).status_code == 403
    headers = {"origin": runtime.frontend_origin}
    result = client.post("/api/auth/recovery/reset", json=body, headers=headers)
    assert result.json() == {"status": "ok"}
    verifier.recover_company_password.assert_called_once_with(
        str(runtime.company_id), body["token"], body["new_password"]
    )
    assert "saas_tenant_session=" in result.headers["set-cookie"]
    verifier.recover_company_password.side_effect = Problem(400, "invalid", "Proof invalid")
    rejected = client.post("/api/auth/recovery/reset", json=body, headers=headers)
    assert rejected.status_code == 400
    assert body["token"] not in rejected.text
    verifier.recover_company_password.side_effect = None
    verifier.recover_company_password.return_value = {"ok": True, "completed": False}
    assert client.post("/api/auth/recovery/reset", json=body, headers=headers).status_code == 503
    native.database.runtime = runtime_for(uuid4())
    verifier.reset_mock()
    assert client.post("/api/auth/recovery/reset", json=body, headers=headers).status_code == 503
    verifier.recover_company_password.assert_not_called()


def test_tenant_child_does_not_share_uds_peer_limit_between_visitors(recovery_app):
    runtime, _, verifier, client = recovery_app
    headers = {"origin": runtime.frontend_origin}
    for _ in range(12):
        assert client.get("/api/auth/recovery/telegram", headers=headers).status_code == 200
    result = client.post(
        "/api/auth/recovery/reset",
        headers=headers,
        json={"token": "b" * 43, "new_password": "synthetic-password"},
    )
    assert result.status_code == 200
    verifier.recover_company_password.assert_called_once()
