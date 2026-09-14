import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.main import create_app
from app.services.iiko_auth import IikoAuthService

TOKEN = "test-stores-token"
BASE = "/api/v1/iiko/stores"
PARENT = str(UUID(int=99))


def store(number=1, code="001", parent=PARENT):
    return (
        f"<corporateItemDto><id>{UUID(int=number)}</id><parentId>{parent}</parentId>"
        f"<code>{code}</code><name>Склад &amp; бар</name><type>STORE</type>"
        "<futureField><value>retained in XML</value></futureField></corporateItemDto>"
    )


def wrap(items):
    return f"<corporateItemDtoes>{items}</corporateItemDtoes>".encode()


def config(**overrides):
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="api-test",
        iiko_password="test-secret",
        **overrides,
    )


class Source:
    def __init__(self, body=None, statuses=None):
        self.body = body if body is not None else wrap(store() + store(2, "", ""))
        self.statuses = iter(statuses or [200] * 10)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/corporation/stores"
        assert dict(request.url.params) == {"revisionFrom": "-1"}
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/xml"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(), iiko_transport=httpx.MockTransport(source), stores_directory=directory
    )


def test_load_pages_raw_and_restore_without_iiko(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE).status_code == 409
        assert not source.requests
        response = client.post(f"{BASE}/load")
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["total"] == 2
        assert snapshot["stores_without_code"] == snapshot["stores_without_parent"] == 1
        assert snapshot["parent_ids"] == [str(UUID(int=99))]
        assert snapshot["revision_from"] == -1 and "include_deleted" not in snapshot
        first = client.get(BASE, params={"limit": 1}).json()["items"][0]
        assert first["id"] == str(UUID(int=1)) and first["parent_id"] == str(UUID(int=99))
        assert first["code"] == "001" and first["name"] == "Склад & бар"
        assert first["taxpayer_id_number"] is None
        assert "deleted" not in first
        second = client.get(BASE, params={"offset": 1, "limit": 1}).json()["items"][0]
        assert second["code"] == "" and second["parent_id"] is None
        assert client.get(BASE, params={"offset": 2}).json()["items"] == []
        assert len(source.requests) == 2
        raw = tmp_path / f"{snapshot['snapshot_id']}.xml"
        assert raw.read_bytes() == source.body
        assert hashlib.sha256(source.body).hexdigest() == snapshot["sha256"]
        assert raw.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "current.json").stat().st_mode & 0o777 == 0o600
        assert TOKEN not in response.text
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert len(source.requests) == 3


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"<corporateItemDtoes>",
        b"<error>private-error</error>",
        b"<corporateItemDtoes><wrong/></corporateItemDtoes>",
        b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><corporateItemDtoes>&x;</corporateItemDtoes>',
        b'<!DOCTYPE x [<!ENTITY x "private-error">]><corporateItemDtoes>&x;</corporateItemDtoes>',
        wrap(store() + store()),
        wrap(store().replace("<type>STORE</type>", "")),
        wrap(store().replace("<type>STORE</type>", "<type>DEPARTMENT</type>")),
        wrap(store().replace(f"<id>{UUID(int=1)}</id>", "<id>not-uuid</id>")),
        wrap(store().replace("<code>001</code>", "<code>001</code><code>002</code>")),
        wrap(store().replace("<code>001</code>", "<code><nested/></code>")),
    ],
)
def test_invalid_xml_does_not_replace_previous_snapshot(tmp_path, body):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
        source.body = body
        response = client.post(f"{BASE}/load")
        assert response.status_code == 502 and "private-error" not in response.text
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert not list(tmp_path.glob("*.part"))


def test_empty_response_and_duplicate_codes_are_valid(tmp_path):
    source = Source(wrap(""))
    with TestClient(app(source, tmp_path)) as client:
        assert client.post(f"{BASE}/load").json()["total"] == 0
        assert client.get(BASE).json()["items"] == []
        source.body = wrap(store() + store(2))
        assert client.post(f"{BASE}/load").json()["total"] == 2


@pytest.mark.parametrize(
    "params", [{"offset": -1}, {"limit": 0}, {"limit": 201}, {"snapshot_id": "bad"}]
)
def test_invalid_pages_never_call_iiko(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 422
        assert not source.requests


def test_old_snapshot_id_detects_a_new_load(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        first = client.post(f"{BASE}/load").json()
        second = client.post(f"{BASE}/load").json()
        assert client.get(BASE, params={"snapshot_id": first["snapshot_id"]}).status_code == 409
        assert client.get(BASE, params={"snapshot_id": second["snapshot_id"]}).status_code == 200


@pytest.mark.parametrize("change", ["bytes", "source", "missing"])
def test_restore_checks_raw_integrity_and_source(tmp_path, change):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
    raw = tmp_path / f"{snapshot['snapshot_id']}.xml"
    settings = config()
    if change == "bytes":
        raw.write_bytes(raw.read_bytes().replace(b"001", b"002"))
    elif change == "missing":
        raw.unlink()
    else:
        settings = settings.model_copy(update={"iiko_login": "different-login"})
    count = len(source.requests)
    with TestClient(app(source, tmp_path, settings)) as client:
        assert client.get(BASE).status_code == (409 if change == "source" else 500)
        assert len(source.requests) == count


def test_size_limit_and_publication_failure_preserve_state(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_stores_max_response_bytes=10))) as client:
        assert client.post(f"{BASE}/load").json()["error"]["code"] == "iiko_stores_too_large"
        assert not list(tmp_path.iterdir())
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
        original = Path.replace

        def broken_publish(path, target):
            if target.name == "current.json":
                raise PermissionError("private-error")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", broken_publish)
        response = client.post(f"{BASE}/load")
        assert response.status_code == 500 and "private-error" not in response.text
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert json.loads((tmp_path / "current.json").read_text())["snapshot"] == snapshot


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_only_401_is_retried_once(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as client:
        response = client.post(f"{BASE}/load")
        assert response.status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/stores") for r in source.requests) == len(statuses)


def test_stores_share_request_lock_with_groups_and_logout(tmp_path):
    async def scenario():
        active = maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"<corporateItemDtoes/>"
                active -= 1

        def handler(request):
            nonlocal active, maximum
            paths.append(request.url.path.rsplit("/", 1)[-1])
            if paths[-1] in {"auth", "logout"}:
                return httpx.Response(200, text=TOKEN)
            active += 1
            maximum = max(active, maximum)
            return httpx.Response(200, stream=Stream())

        settings = config()
        auth = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            await asyncio.gather(
                auth.download_stores(tmp_path / "stores.xml"),
                auth.download_groups(tmp_path / "groups.json"),
                auth.logout(),
            )
            assert maximum == 1 and paths == ["auth", "stores", "list", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_store_timeout_keeps_session_available_for_logout(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/stores"):
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_stores_timeout_seconds=0.01))) as client:
        assert client.post(f"{BASE}/load").status_code == 504
        assert not list(tmp_path.iterdir())
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
