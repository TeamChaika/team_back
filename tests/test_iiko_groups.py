import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.schemas.iiko_groups import IikoGroup
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_groups import summarize_groups

TOKEN = "test-group-token"
BASE = "/api/v1/iiko/groups"


def config(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="api-test",
        iiko_password="test-secret",
        **overrides,
    )


def group(number: int = 1, parent: int | None = None) -> dict:
    return {
        "id": str(UUID(int=number)),
        "name": f"Группа {number}",
        "num": "00012",
        "code": "003",
        "deleted": False,
        "parent": str(UUID(int=parent)) if parent else None,
        "category": str(UUID(int=99)),
        "description": None,
        "visibilityFilter": {"departments": [str(UUID(int=88))], "excluding": True},
        "futureField": "retained in raw",
    }


class Source:
    def __init__(self):
        self.paths = []
        self.body = json.dumps([group(1), group(2, 1)]).encode()

    def __call__(self, request: httpx.Request):
        self.paths.append(request.url.path)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/v2/entities/products/group/list"
        assert dict(request.url.params) == {"includeDeleted": "false"}
        assert request.headers["cookie"] == f"key={TOKEN}"
        return httpx.Response(200, content=self.body)


def app(source, directory: Path, settings: Settings | None = None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        groups_directory=directory,
    )


def test_groups_load_preserves_ids_hierarchy_raw_and_restart(tmp_path: Path) -> None:
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE).status_code == 409
        assert source.paths == []
        response = client.post(f"{BASE}/load")
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["total"] == 2
        assert snapshot["root_groups"] == 1
        assert snapshot["missing_parent_ids"] == snapshot["cycle_group_ids"] == []
        first = client.get(BASE, params={"limit": 1}).json()["items"][0]
        second = client.get(BASE, params={"offset": 1, "limit": 1}).json()["items"][0]
        assert first["parent_id"] is None
        assert second["parent_id"] == first["id"]
        assert second["category_id"] != second["parent_id"]
        assert second["num"] == "00012" and second["code"] == "003"
        assert second["visibility_filter"]["excluding"] is True
        assert len(source.paths) == 2
        raw = tmp_path / f"{snapshot['snapshot_id']}.json"
        assert raw.read_bytes() == source.body
        assert raw.stat().st_mode & 0o777 == 0o600
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
        assert client.get(BASE).status_code == 200
        assert len(source.paths) == 3
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert len(source.paths) == 3


def test_hierarchy_reports_orphans_cycles_and_only_actual_cycle_members() -> None:
    values = [
        group(1),
        group(2, 1),
        group(3, 80),
        group(4, 5),
        group(5, 4),
        group(6, 4),
        group(7, 7),
    ]
    result = summarize_groups([IikoGroup.model_validate(value) for value in values])
    assert result == {
        "root_groups": 1,
        "missing_parent_ids": [UUID(int=80)],
        "cycle_group_ids": [UUID(int=4), UUID(int=5), UUID(int=7)],
    }


def test_deep_hierarchy_does_not_use_recursion() -> None:
    groups = [
        IikoGroup.model_validate(group(n, n + 1 if n < 2000 else None)) for n in range(1, 2001)
    ]
    assert summarize_groups(groups) == {
        "root_groups": 1,
        "missing_parent_ids": [],
        "cycle_group_ids": [],
    }


@pytest.mark.parametrize("body", [b"{}", b"[", b"[null]", b"[] trailing"])
def test_broken_group_response_preserves_previous_snapshot(tmp_path: Path, body: bytes) -> None:
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
        source.body = body
        response = client.post(f"{BASE}/load")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_groups_invalid_response"
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("invalid", ["duplicate", "deleted", "missing_parent", "numeric_num"])
def test_invalid_group_is_not_silently_dropped(tmp_path: Path, invalid: str) -> None:
    value = group()
    values = [value]
    if invalid == "duplicate":
        values.append(group())
    elif invalid == "deleted":
        value["deleted"] = True
    elif invalid == "missing_parent":
        del value["parent"]
    else:
        value["num"] = 12
    source = Source()
    source.body = json.dumps(values).encode()
    with TestClient(app(source, tmp_path)) as client:
        assert client.post(f"{BASE}/load").status_code == 502
        assert client.get(BASE).status_code == 409


def test_group_pages_validate_limits_and_snapshot_id(tmp_path: Path) -> None:
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        for params in [{"limit": 201}, {"limit": 0}, {"offset": -1}]:
            assert client.get(BASE, params=params).status_code == 422
        assert source.paths == []
        snapshot = client.post(f"{BASE}/load").json()
        assert client.get(BASE, params={"offset": 2}).json()["items"] == []
        assert client.get(BASE, params={"snapshot_id": str(UUID(int=10))}).status_code == 409
        assert client.get(BASE, params={"snapshot_id": snapshot["snapshot_id"]}).status_code == 200


def test_group_size_limit_is_separate_from_products(tmp_path: Path) -> None:
    with TestClient(app(Source(), tmp_path, config(iiko_groups_max_response_bytes=16))) as client:
        response = client.post(f"{BASE}/load")
        assert response.json()["error"]["code"] == "iiko_groups_too_large"
        assert not list(tmp_path.iterdir())
        assert client.get("/api/v1/iiko/status").json()["state"] == "token_cached"


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403]])
def test_group_session_renewal_is_bounded(tmp_path: Path, statuses: list[int]) -> None:
    calls = []
    remaining = iter(statuses)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(next(remaining), content=b"[]")

    with TestClient(app(handler, tmp_path)) as client:
        response = client.post(f"{BASE}/load")
        assert response.status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(path.endswith("/auth") for path in calls) == (2 if statuses[0] == 401 else 1)
        assert sum(path.endswith("/group/list") for path in calls) == len(statuses)


def test_products_groups_and_logout_share_one_request_lock(tmp_path: Path) -> None:
    async def scenario():
        active = 0
        maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"[]"
                active -= 1

        def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            paths.append(request.url.path)
            if request.url.path.endswith(("/auth", "/logout")):
                active -= 1
                return httpx.Response(200, text=TOKEN)
            return httpx.Response(200, stream=Stream())

        settings = config()
        auth = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            await asyncio.gather(
                auth.download_groups(tmp_path / "groups.json"),
                auth.download_products(tmp_path / "products.json"),
                auth.logout(),
            )
            assert maximum == 1
            assert paths == [
                "/resto/api/auth",
                "/resto/api/v2/entities/products/group/list",
                "/resto/api/v2/entities/products/list",
                "/resto/api/logout",
            ]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_groups_use_their_own_timeout(tmp_path: Path) -> None:
    async def scenario():
        async def handler(request):
            if request.url.path.endswith("/list"):
                await asyncio.Event().wait()
            return httpx.Response(200, text=TOKEN)

        settings = config(iiko_groups_timeout_seconds=0.02)
        auth = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            with pytest.raises(IikoError) as failure:
                await auth.download_groups(tmp_path / "groups.json")
            assert failure.value.code == "iiko_timeout"
            assert auth.status().state == "token_cached"
        finally:
            await auth.aclose()

    asyncio.run(scenario())
