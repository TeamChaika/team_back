import asyncio
import hashlib
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.services.iiko_auth import IikoAuthService

TOKEN = "b354d18c-3d3a-e1a6-c3b9-9ef7b5055318"
PASSWORD = "test-пароль#&+"


def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api/",
        iiko_login="api user+test",
        iiko_password=PASSWORD,
    )


@pytest.mark.parametrize("logout_prefix", ["", "Connection released: "])
def test_auth_reuses_token_logout_releases_and_shutdown_logs_out(logout_prefix: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/auth"):
            assert request.url.params["login"] == "api user+test"
            assert (
                request.url.params["pass"]
                == hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest()
            )
            assert PASSWORD not in str(request.url)
            return httpx.Response(200, text=TOKEN, headers={"set-cookie": f"key={TOKEN}; Path=/"})
        assert request.url.path == "/resto/api/logout"
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert not request.url.query
        return httpx.Response(200, text=f"{logout_prefix}{TOKEN}")

    application = create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    with TestClient(application) as client:
        assert not requests  # Ни запуск, ни status не занимают лицензию.
        assert client.get("/api/v1/iiko/status").json()["state"] == "logged_out"
        assert not requests
        first = client.post("/api/v1/iiko/auth")
        assert first.status_code == 200
        assert first.json() == {"configured": True, "state": "token_cached", "reused": False}
        assert TOKEN not in first.text
        second = client.post("/api/v1/iiko/auth")
        assert second.json()["reused"] is True
        assert len(requests) == 1
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
        assert len(requests) == 2
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
        assert len(requests) == 2
        assert client.post("/api/v1/iiko/auth").json()["reused"] is False
    assert [request.url.path for request in requests] == [
        "/resto/api/auth",
        "/resto/api/logout",
        "/resto/api/auth",
        "/resto/api/logout",
    ]


def test_missing_configuration_makes_no_network_requests() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unconfigured application must not access iiko")

    settings = Settings(_env_file=None, iiko_base_url=None, iiko_login="", iiko_password="")
    with TestClient(create_app(settings, iiko_transport=httpx.MockTransport(unexpected))) as client:
        response = client.post("/api/v1/iiko/auth")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "iiko_not_configured"
        assert client.get("/api/v1/iiko/status").json()["configured"] is False


@pytest.mark.parametrize("upstream_status", [401, 403, 429, 500])
def test_upstream_errors_are_safe_and_not_retried(upstream_status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(upstream_status, text=f"Sensitive body: {PASSWORD} {TOKEN}")

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post("/api/v1/iiko/auth")
        assert response.status_code == 502
        assert str(upstream_status) in response.json()["error"]["message"]
        assert PASSWORD not in response.text
        assert TOKEN not in response.text
        assert len(requests) == 1
        expected = "unknown" if upstream_status == 500 else "logged_out"
        assert client.get("/api/v1/iiko/status").json()["state"] == expected


def test_timeout_blocks_duplicate_auth_when_license_outcome_is_unknown() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout(f"Secret URL: {request.url}", request=request)

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post("/api/v1/iiko/auth")
        assert response.status_code == 504
        assert "pass=" not in response.text
        assert client.post("/api/v1/iiko/auth").status_code == 409
        assert client.post("/api/v1/iiko/logout").status_code == 409
        assert client.get("/api/v1/iiko/status").json()["state"] == "unknown"
        assert len(requests) == 1


def test_failed_logout_preserves_token_and_allows_logout_retry() -> None:
    logout_attempts = 0
    auth_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logout_attempts, auth_attempts
        if request.url.path.endswith("/auth"):
            auth_attempts += 1
            return httpx.Response(200, text=TOKEN)
        logout_attempts += 1
        assert request.headers["cookie"] == f"key={TOKEN}"
        if logout_attempts == 1:
            raise httpx.ReadTimeout("Timed out", request=request)
        return httpx.Response(200, text=TOKEN)

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        assert client.post("/api/v1/iiko/auth").status_code == 200
        assert client.post("/api/v1/iiko/logout").status_code == 504
        assert client.post("/api/v1/iiko/auth").status_code == 409
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
        assert (auth_attempts, logout_attempts) == (1, 2)


@pytest.mark.parametrize("body", ["", "<html>login</html>", "a" * 4097, "token;injection"])
def test_invalid_token_is_not_accepted_or_retried(body: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        response = client.post("/api/v1/iiko/auth")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_invalid_response"
        assert client.post("/api/v1/iiko/auth").status_code == 409


def test_redirect_is_not_followed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/"})

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        assert client.post("/api/v1/iiko/auth").status_code == 502
        assert len(requests) == 1


def test_logout_must_confirm_the_same_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(200, text="Connection released: different-token")

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        assert client.post("/api/v1/iiko/auth").status_code == 200
        assert client.post("/api/v1/iiko/logout").status_code == 502
        assert client.get("/api/v1/iiko/status").json()["state"] == "unknown"


def test_concurrent_auth_and_logout_requests_are_serialized() -> None:
    async def scenario() -> None:
        active = 0
        maximum_active = 0
        paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            paths.append(request.url.path)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, text=TOKEN)

        settings = configured_settings()
        service = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            results = await asyncio.gather(*(service.authenticate() for _ in range(10)))
            assert sum(not result.reused for result in results) == 1
            assert paths == ["/resto/api/auth"]
            await asyncio.gather(service.logout(), service.authenticate(), service.logout())
            assert maximum_active == 1
            assert paths == [
                "/resto/api/auth",
                "/resto/api/logout",
                "/resto/api/auth",
                "/resto/api/logout",
            ]
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_cancelled_auth_does_not_request_another_token() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            return httpx.Response(200, text=TOKEN)

        settings = configured_settings()
        service = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        task = asyncio.create_task(service.authenticate())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.status().state == "unknown"
        with pytest.raises(IikoError, match="Новый токен не запрашивается"):
            await service.authenticate()
        await service.aclose()

    asyncio.run(scenario())


def test_httpx_logs_do_not_expose_credentials(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=TOKEN)

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        assert client.post("/api/v1/iiko/auth").status_code == 200
    assert "[REDACTED]" in caplog.text
    assert PASSWORD not in caplog.text
    assert TOKEN not in caplog.text
    assert hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest() not in caplog.text


def test_total_timeout_limits_an_unresponsive_transport() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()
            return httpx.Response(200, text=TOKEN)

        settings = configured_settings().model_copy(update={"iiko_timeout_seconds": 0.02})
        service = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            with pytest.raises(IikoError) as failure:
                await asyncio.wait_for(service.authenticate(), timeout=1)
            assert failure.value.code == "iiko_timeout"
            assert service.status().state == "unknown"
        finally:
            await service.aclose()

    asyncio.run(scenario())


def test_shutdown_failure_is_visible_and_does_not_leak_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(500, text=f"secret={TOKEN}")

    with TestClient(
        create_app(configured_settings(), iiko_transport=httpx.MockTransport(handler))
    ) as client:
        assert client.post("/api/v1/iiko/auth").status_code == 200
    assert "Выход из iiko при остановке не подтверждён" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://iiko.example/resto/api",
        "https://user:secret@iiko.example/resto/api",
        "https://iiko.example/resto/api?key=secret",
        "https://iiko.example/resto/api#fragment",
        "https://iiko.example/",
    ],
)
def test_unsafe_or_incomplete_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, iiko_base_url=url)
