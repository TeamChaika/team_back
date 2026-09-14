"""Transient Auth failures recover without replaying uncertain token rotation."""

import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException

from app.web.auth import Auth
from app.web.settings import WebSettings

SECRET = "private-password-token-should-not-appear"


def call(handler, method="GET", path="user", **kwargs):
    async def run():
        auth = Auth(
            WebSettings(
                _env_file=None, supabase_url="https://auth.example.invalid", anon_key=SECRET
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await auth.call(method, path, **kwargs)
        finally:
            await auth.client.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connect_failure_retries_password_login_once_without_logging_credentials(error, caplog):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise error(SECRET, request=request)
        return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh"})

    assert (
        call(handler, "POST", "token?grant_type=password", json={"password": SECRET})[
            "access_token"
        ]
        == "access"
    )
    assert len(attempts) == 2
    assert SECRET not in caplog.text
    assert "operation=token" in caplog.text and "retry=True" in caplog.text


@pytest.mark.parametrize("failure", [502, 503, 504, httpx.RemoteProtocolError, httpx.ReadError])
def test_user_check_retries_transient_error_once(failure):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            if isinstance(failure, int):
                return httpx.Response(failure, text=SECRET)
            raise failure(SECRET, request=request)
        return httpx.Response(200, json={"id": "confirmed-user"})

    assert call(handler) == {"id": "confirmed-user"}
    assert len(attempts) == 2


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.RemoteProtocolError, 502, 503])
def test_uncertain_refresh_token_post_is_never_replayed(failure, caplog):
    attempts = []

    def handler(request):
        attempts.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, text=SECRET)
        raise failure(SECRET, request=request)

    with pytest.raises(HTTPException) as exc:
        call(handler, "POST", "token?grant_type=refresh_token", json={"refresh_token": SECRET})
    assert exc.value.status_code == 503
    assert len(attempts) == 1
    assert SECRET not in caplog.text and SECRET not in exc.value.detail


@pytest.mark.parametrize("status,expected", [(400, 401), (401, 401), (429, 429), (302, 503)])
def test_credential_errors_rate_limits_and_redirects_are_not_retried(status, expected):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, text=SECRET)

    with pytest.raises(HTTPException) as exc:
        call(handler, "POST", "token?grant_type=password")
    assert exc.value.status_code == expected
    assert len(attempts) == 1


def test_persistent_network_error_is_bounded_and_sanitized(caplog):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError(SECRET, request=request)

    with pytest.raises(HTTPException) as exc:
        call(handler)
    assert exc.value.status_code == 503
    assert len(attempts) == 2
    assert SECRET not in caplog.text and SECRET not in exc.value.detail


def test_retry_budget_has_one_overall_deadline(monkeypatch, caplog):
    monkeypatch.setattr("app.web.auth.AUTH_REQUEST_TIMEOUT", 0.01)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    started = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        call(handler)
    assert exc.value.status_code == 503
    assert time.monotonic() - started < 0.5
    assert "reason=deadline" in caplog.text
