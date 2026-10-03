"""Self-service account changes use the caller's identity, never admin credentials."""

import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.portal import create_portal
from app.web.auth import ACCESS_COOKIE
from app.web.settings import WebSettings
from tests.test_portal import OTHER, USER, FakeRepository

ORIGIN = {"Origin": "http://127.0.0.1:8013"}
PASSWORDS = {"current_password": "old-private", "new_password": "new-private-password"}


@pytest.fixture
def client():
    calls = []
    state = {"reauth_id": USER, "wrong_password": False, "update_failure": None}

    def provider(request):
        calls.append(request)
        if request.method == "GET":
            if request.headers.get("authorization") not in {"Bearer session", "Bearer fresh"}:
                return httpx.Response(401)
            uid = state["reauth_id"] if request.headers["authorization"] == "Bearer fresh" else USER
            return httpx.Response(200, json={"id": str(uid), "email": "self@example.invalid"})
        if request.url.path.endswith("/token"):
            if state["wrong_password"]:
                return httpx.Response(400, json={"msg": "private upstream detail"})
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "fresh-r"})
        if request.method == "PUT":
            if state["update_failure"]:
                return httpx.Response(state["update_failure"], json={"msg": "secret detail"})
            return httpx.Response(200, json={"id": str(USER)})
        return httpx.Response(204)

    repo = FakeRepository()
    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=repo,
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        client.calls, client.state, client.repo = calls, state, repo
        yield client


def authenticated(client):
    client.cookies.set(ACCESS_COOKIE, "session")


def test_requires_session_origin_and_active_profile(client):
    path = "/api/profile/password"
    assert client.post(path, headers=ORIGIN, json=PASSWORDS).status_code == 401
    authenticated(client)
    assert client.post(path, json=PASSWORDS).status_code == 403
    client.repo.active = False
    assert client.post(path, headers=ORIGIN, json=PASSWORDS).status_code == 403
    assert not any(r.method == "PUT" for r in client.calls)


@pytest.mark.parametrize("new_password", ["a" * 8, "a" * 11, "a" * 128])
def test_password_uses_verified_self_email_and_rotates_cookies(client, new_password):
    authenticated(client)
    response = client.post(
        "/api/profile/password", headers=ORIGIN, json={**PASSWORDS, "new_password": new_password}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    login = next(r for r in client.calls if r.url.path.endswith("/token"))
    assert json.loads(login.content) == {"email": "self@example.invalid", "password": "old-private"}
    update = next(r for r in client.calls if r.method == "PUT")
    assert update.headers["authorization"] == "Bearer fresh"
    assert json.loads(update.content) == {"password": new_password}
    assert "fresh-r" in response.headers["set-cookie"]
    assert "Cache-Control" in response.headers


@pytest.mark.parametrize(
    "payload",
    [
        {**PASSWORDS, "new_password": "a" * 7},
        {**PASSWORDS, "new_password": "a" * 129},
        {**PASSWORDS, "user_id": str(OTHER)},
        {"current_password": "same-password", "new_password": "same-password"},
    ],
)
def test_password_validation_does_not_expose_secrets(client, payload):
    authenticated(client)
    response = client.post("/api/profile/password", headers=ORIGIN, json=payload)
    assert response.status_code == 422
    assert payload["current_password"] not in response.text
    assert not any(r.method == "PUT" for r in client.calls)


def test_wrong_password_does_not_trigger_session_refresh_or_update(client):
    authenticated(client)
    client.state["wrong_password"] = True
    response = client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS)
    assert response.status_code == 422
    assert "private upstream" not in response.text
    assert not any(r.method == "PUT" for r in client.calls)


def test_reauthentication_cannot_switch_accounts(client):
    authenticated(client)
    client.state["reauth_id"] = OTHER
    assert client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS).status_code == 403
    assert not any(r.method == "PUT" for r in client.calls)


@pytest.mark.parametrize("status, expected", [(400, 422), (500, 503)])
def test_failed_update_is_not_reported_as_success(client, status, expected):
    authenticated(client)
    client.state["update_failure"] = status
    response = client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS)
    assert response.status_code == expected
    assert "secret detail" not in response.text
    assert "set-cookie" not in response.headers


def test_password_attempts_are_limited_per_account(client):
    authenticated(client)
    client.state["wrong_password"] = True
    for _ in range(5):
        assert (
            client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS).status_code == 422
        )
    assert client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS).status_code == 429


def test_telegram_disabled_status_and_origin_guard(client):
    path = "/api/profile/telegram"
    assert client.get(path).status_code == 401
    authenticated(client)
    assert client.get(path).json() == {"available": False, "linked": False, "telegram_id": None}
    assert client.post(path + "/link").status_code == 403
    assert client.post(path + "/link", headers=ORIGIN).status_code == 503


def test_required_password_is_cleared_only_after_confirmed_update(client):
    authenticated(client)
    client.repo.password_change_required = True
    client.state["wrong_password"] = True
    assert client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS).status_code == 422
    assert client.repo.password_change_required
    client.state["wrong_password"] = False
    client.state["update_failure"] = 503
    assert client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS).status_code == 503
    assert client.repo.password_change_required
    client.state["update_failure"] = None
    assert client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS).status_code == 200
    assert not client.repo.password_change_required
    assert client.get("/api/purchase-prices").status_code == 200


def test_database_failure_after_auth_update_keeps_gate_and_explains_partial_success(client):
    authenticated(client)
    client.repo.password_change_required = True

    def unavailable(user_id):
        raise psycopg.OperationalError("private database detail")

    client.repo.complete_password_change = unavailable
    response = client.post("/api/profile/password", headers=ORIGIN, json=PASSWORDS)
    assert response.status_code == 503
    assert "Новый пароль сохранён" in response.json()["detail"]
    assert "private" not in response.text
    assert "fresh-r" in response.headers["set-cookie"]
    assert client.repo.password_change_required
    assert client.get("/api/purchase-prices").json()["detail"]["code"] == "password_change_required"
