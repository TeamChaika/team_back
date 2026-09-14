import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.iiko_connections import read_connections
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_connections import IikoConnectionsService

BASE = "/api/v1/iiko/connections"
PASSWORD = "private-test-password"


def definition(connection_id="rms-one", **overrides):
    return {
        "id": connection_id,
        "label": "Test RMS",
        "base_url": f"https://{connection_id}.iiko.example/resto/api",
        "use_primary_credentials": True,
        **overrides,
    }


def config(tmp_path, definitions=None, raw=None, **overrides):
    path = tmp_path / "connections.json"
    path.write_text(
        raw
        if raw is not None
        else json.dumps(
            {
                "connections": [definition()] if definitions is None else definitions,
            }
        )
    )
    return Settings(
        _env_file=None,
        iiko_base_url="https://primary.iiko.example/resto/api",
        iiko_login="api-test",
        iiko_password=PASSWORD,
        iiko_connections_file=path,
        **overrides,
    )


class Source:
    def __init__(self, payload=None, statuses=None):
        self.payload = payload
        self.statuses = iter(statuses or [200] * 20)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        host = request.url.host
        token = f"token-{host}"
        assert request.method == "GET"
        if request.url.path.endswith("/auth"):
            assert not request.headers.get("cookie")
            assert (
                request.url.params["pass"]
                == hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest()
            )
            return httpx.Response(
                200,
                text=token,
                headers={"set-cookie": f"key={token}; Domain=.iiko.example; Path=/"},
            )
        assert request.headers["cookie"] == f"key={token}"
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, text=token)
        assert request.url.path == "/resto/api/replication/serverType" and not request.url.query
        payload = self.payload
        if payload is None:
            payload = b'"CHAIN"' if host.startswith("primary.") else b"REPLICATED_RMS"
        return httpx.Response(next(self.statuses), content=payload)


def app(settings, source, tmp_path):
    return create_app(
        settings,
        iiko_transport=httpx.MockTransport(source),
        connections_directory=tmp_path / "snapshots",
    )


def test_connections_are_local_and_primary_reuses_existing_session(tmp_path):
    source = Source()
    settings = config(tmp_path)
    with TestClient(app(settings, source, tmp_path)) as c:
        initial = c.get(BASE).json()["items"]
        assert [item["connection_id"] for item in initial] == ["primary", "rms-one"]
        assert all(item["last_verified_type"] is None for item in initial)
        assert all(item["configured"] and item["state"] == "logged_out" for item in initial)
        assert not source.requests
        assert c.post("/api/v1/iiko/auth").status_code == 200
        primary = c.get(f"{BASE}/primary/server-type").json()
        assert primary["server_type"] == "CHAIN"
        first = c.get(f"{BASE}/rms-one/server-type").json()
        second = c.get(f"{BASE}/rms-one/server-type").json()
        assert first["server_type"] == "REPLICATED_RMS"
        assert first["snapshot_id"] != second["snapshot_id"]
        assert sum(r.url.path.endswith("/auth") for r in source.requests) == 2
        after = c.get(BASE)
        assert (
            PASSWORD not in after.text
            and "api-test" not in after.text
            and "token-" not in after.text
        )
        assert all(item["state"] == "token_cached" for item in after.json()["items"])
        assert c.post(f"{BASE}/rms-one/logout").json()["state"] == "logged_out"
        assert c.get("/api/v1/iiko/status").json()["state"] == "token_cached"
        assert c.post(f"{BASE}/primary/logout").json()["state"] == "logged_out"
        assert c.get("/api/v1/iiko/status").json()["state"] == "logged_out"
    for item, raw in [
        (primary, b'"CHAIN"'),
        (first, b"REPLICATED_RMS"),
        (second, b"REPLICATED_RMS"),
    ]:
        directory = tmp_path / "snapshots" / item["connection_id"]
        raw_path = directory / f"{item['snapshot_id']}.txt"
        meta_path = directory / f"{item['snapshot_id']}.meta.json"
        assert raw_path.read_bytes() == raw
        assert (
            item["source_bytes"] == len(raw) and item["sha256"] == hashlib.sha256(raw).hexdigest()
        )
        assert raw_path.stat().st_mode & 0o777 == meta_path.stat().st_mode & 0o777 == 0o600
        assert directory.stat().st_mode & 0o777 == 0o700
        assert "token-" not in meta_path.read_text() and PASSWORD not in meta_path.read_text()
        assert json.loads(meta_path.read_text())["connection_id"] == item["connection_id"]


def test_no_implicit_credential_reuse_and_individual_credentials_are_supported(tmp_path):
    settings = config(
        tmp_path,
        [
            definition(use_primary_credentials=False),
            definition(
                "rms-two", use_primary_credentials=False, login="own-login", password="own-secret"
            ),
        ],
    )
    definitions = read_connections(settings)
    assert not definitions[0][1].iiko_configured
    assert definitions[1][1].iiko_login == "own-login"
    assert definitions[1][1].iiko_password.get_secret_value() == "own-secret"
    source = Source()
    with TestClient(app(settings, source, tmp_path)) as c:
        assert c.get(f"{BASE}/rms-one/server-type").status_code == 503
        assert not source.requests


@pytest.mark.parametrize(
    "definitions",
    [
        [definition("primary")],
        [definition(), definition()],
        [definition(base_url="https://primary.iiko.example:443/resto/api/")],
        [definition(), definition("rms-two", base_url="https://RMS-ONE.iiko.example/resto/api/")],
        [definition("../outside")],
        [definition(base_url="http://rms-one.iiko.example/resto/api")],
        [definition(base_url="https://rms-one.iiko.example/other")],
        [definition(base_url="https://login:private-secret@rms-one.iiko.example/resto/api")],
        [definition(base_url="https://rms-one.iiko.example/resto/api?key=private-secret")],
        [definition(use_primary_credentials=True, password="private-secret")],
        [definition(unknown="private-secret")],
    ],
)
def test_invalid_or_duplicate_servers_fail_without_network_or_secret_details(tmp_path, definitions):
    source = Source()
    with pytest.raises(IikoError) as captured:
        with TestClient(app(config(tmp_path, definitions), source, tmp_path)):
            pytest.fail("Invalid configuration accepted")
    assert captured.value.code == "iiko_connections_config_error"
    assert "private-secret" not in str(captured.value) and not source.requests


@pytest.mark.parametrize("raw", ["{", "[]", '{"connections":[],"connections":[]}', "x" * 262145])
def test_invalid_config_file_is_safe(tmp_path, raw):
    with pytest.raises(IikoError, match="локальный файл"):
        read_connections(config(tmp_path, raw=raw))


def test_missing_config_file_is_not_silently_ignored(tmp_path):
    settings = config(tmp_path)
    settings.iiko_connections_file.unlink()
    with pytest.raises(IikoError):
        read_connections(settings)
    assert read_connections(Settings(_env_file=None, iiko_connections_file=None)) == []


@pytest.mark.parametrize(
    "payload", [b"", b"RMS", b"{}", b"[]", b"null", b'"CHAIN" extra', b"private-secret", b"\xff"]
)
def test_invalid_server_type_does_not_replace_last_success_or_leak(tmp_path, payload):
    source = Source()
    with TestClient(app(config(tmp_path), source, tmp_path)) as c:
        assert c.get(f"{BASE}/rms-one/server-type").status_code == 200
        source.payload = payload
        r = c.get(f"{BASE}/rms-one/server-type")
        assert (
            r.status_code == 502
            and r.json()["error"]["code"] == "iiko_server_type_invalid_response"
        )
        assert "private-secret" not in r.text
        item = c.get(BASE).json()["items"][1]
        assert item["last_verified_type"] == "REPLICATED_RMS" and item["type_verified_at"]
        assert len(list((tmp_path / "snapshots/rms-one").iterdir())) == 2


@pytest.mark.parametrize("payload", [b"CHAIN", b'"REPLICATED_RMS"', b"STANDALONE_RMS\n"])
def test_server_type_formats(tmp_path, payload):
    with TestClient(app(config(tmp_path), Source(payload), tmp_path)) as c:
        r = c.get(f"{BASE}/rms-one/server-type")
        assert r.status_code == 200 and r.json()["server_type"] == payload.decode().strip().strip(
            '"'
        )


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_only_401_reauthenticates_selected_connection(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(config(tmp_path), source, tmp_path)) as c:
        r = c.get(f"{BASE}/rms-one/server-type")
        assert r.status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/serverType") for r in source.requests) == len(statuses)
        assert all(r.url.host == "rms-one.iiko.example" for r in source.requests)


def test_unknown_connection_cannot_choose_an_arbitrary_server(tmp_path):
    source = Source()
    with TestClient(app(config(tmp_path), source, tmp_path)) as c:
        assert c.get(f"{BASE}/unconfigured/server-type").status_code == 404
        assert c.post(f"{BASE}/unconfigured/logout").status_code == 404
        assert not source.requests


def test_size_and_disk_errors_clean_partial_files(tmp_path, monkeypatch):
    with TestClient(app(config(tmp_path), Source(b"x" * 4097), tmp_path)) as c:
        assert (
            c.get(f"{BASE}/primary/server-type").json()["error"]["code"]
            == "iiko_server_type_too_large"
        )
        assert not list((tmp_path / "snapshots/primary").iterdir())

    def broken_replace(self, target):
        raise PermissionError("private-secret")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(config(tmp_path), Source(), tmp_path)) as c:
        r = c.get(f"{BASE}/primary/server-type")
        assert r.status_code == 500 and "private-secret" not in r.text
        assert not list((tmp_path / "snapshots/primary").iterdir())


def test_timeout_is_isolated_and_shutdown_closes_each_session(tmp_path):
    source = Source()

    async def handler(request):
        if request.url.host.startswith("rms-one.") and request.url.path.endswith("/serverType"):
            await asyncio.sleep(1)
        return source(request)

    with TestClient(app(config(tmp_path, iiko_timeout_seconds=0.02), handler, tmp_path)) as c:
        assert c.get(f"{BASE}/primary/server-type").status_code == 200
        assert c.get(f"{BASE}/rms-one/server-type").status_code == 504
        assert c.get(f"{BASE}/primary/server-type").status_code == 200
    assert {r.url.host for r in source.requests if r.url.path.endswith("/logout")} == {
        "primary.iiko.example",
        "rms-one.iiko.example",
    }


def test_primary_probe_and_existing_download_use_same_request_lock(tmp_path):
    async def run():
        active = maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"CHAIN"
                active -= 1

        def handler(request):
            nonlocal active, maximum
            path = request.url.path.rsplit("/", 1)[-1]
            paths.append(path)
            if path in {"auth", "logout"}:
                return httpx.Response(200, text="test-token")
            active += 1
            maximum = max(active, maximum)
            return httpx.Response(200, stream=Stream())

        settings = config(tmp_path, [])
        transport = httpx.MockTransport(handler)
        primary = IikoAuthService(settings, IikoClient(settings, transport))
        registry = IikoConnectionsService(
            settings, primary, transport=transport, directory=tmp_path / "snapshots"
        )
        try:
            await asyncio.gather(
                registry.get_server_type("primary"),
                primary.download_employees(tmp_path / "employees.xml"),
                registry.logout("primary"),
            )
            assert maximum == 1 and paths == ["auth", "serverType", "employees", "logout"]
        finally:
            await registry.aclose()
            await primary.aclose()

    asyncio.run(run())
