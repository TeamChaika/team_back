import asyncio
import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.services.iiko_auth import IikoAuthService
from app.services.iiko_products import IikoProductsService

TOKEN = "test-token"
PASSWORD = "private-password"
BASE = "/api/v1/iiko/products"


def settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="test-api",
        iiko_password=PASSWORD,
        **overrides,
    )


def product(number: int = 1) -> dict:
    return {
        "id": str(UUID(int=number)),
        "name": f"Тестовый товар {number}",
        "num": "00012",
        "code": "001",
        "type": "GOODS",
        "deleted": False,
        "parent": None,
        "mainUnit": str(UUID(int=100)),
        "defaultSalePrice": 123,
        "unitWeight": 1,
        "unitCapacity": 0,
        "containers": [],
        "barcodes": None,
        "extraFieldFromFutureIiko": {"keep": "in raw"},
    }


class Source:
    def __init__(self, body: bytes | None = None) -> None:
        self.body = body if body is not None else json.dumps([product()]).encode()
        self.status = 200
        self.paths: list[str] = []
        self.product_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        self.product_requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/v2/entities/products/list"
        assert dict(request.url.params) == {"includeDeleted": "false"}
        assert request.headers["cookie"] == f"key={TOKEN}"
        return httpx.Response(self.status, content=self.body)


def application(source: Source, directory: Path, config: Settings | None = None):
    return create_app(
        config if config is not None else settings(),
        iiko_transport=httpx.MockTransport(source),
        products_directory=directory,
    )


def test_load_pages_raw_persistence_and_restart(tmp_path: Path) -> None:
    source = Source(json.dumps([product(1), product(2)], ensure_ascii=False).encode())
    with TestClient(application(source, tmp_path)) as client:
        assert source.paths == []
        assert client.get(BASE).status_code == 409
        assert source.paths == []
        response = client.post(f"{BASE}/load")
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["total"] == 2
        assert snapshot["type_counts"] == {"GOODS": 2}
        assert snapshot["sha256"] == hashlib.sha256(source.body).hexdigest()
        assert snapshot["source_bytes"] == len(source.body)
        first = client.get(BASE, params={"limit": 1}).json()
        second = client.get(
            BASE, params={"offset": 1, "limit": 1, "snapshot_id": snapshot["snapshot_id"]}
        ).json()
        assert first["items"][0]["id"] != second["items"][0]["id"]
        assert first["items"][0]["num"] == second["items"][0]["num"] == "00012"
        assert first["items"][0]["code"] == "001"
        assert first["items"][0]["default_sale_price"] == "123"
        assert first["items"][0]["parent_id"] is None
        assert client.get(BASE, params={"offset": 99}).json()["items"] == []
        assert len(source.product_requests) == 1
        raw = tmp_path / f"{snapshot['snapshot_id']}.json"
        assert raw.read_bytes() == source.body
        assert raw.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "current.json").stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert TOKEN not in response.text
    requests_before_restart = len(source.paths)
    with TestClient(application(source, tmp_path)) as client:
        assert client.get(BASE).json()["snapshot"] == snapshot
        assert client.get("/api/v1/iiko/status").json()["state"] == "logged_out"
    assert len(source.paths) == requests_before_restart


def test_decimal_precision_and_nested_fields(tmp_path: Path) -> None:
    item = product()
    item["type"] = "FUTURE_TYPE"
    item["containers"] = [
        {"id": str(UUID(int=10)), "name": "Упаковка", "num": "02", "count": 1, "deleted": False}
    ]
    item["barcodes"] = [{"barcode": "0012345", "containerId": str(UUID(int=10))}]
    body = json.dumps([item]).replace(
        '"defaultSalePrice": 123', '"defaultSalePrice": 0.123456789123456789'
    )
    with TestClient(application(Source(body.encode()), tmp_path)) as client:
        assert client.post(f"{BASE}/load").status_code == 200
        result = client.get(BASE).json()["items"][0]
        assert result["default_sale_price"] == "0.123456789123456789"
        assert result["containers"][0]["num"] == "02"
        assert result["containers"][0]["count"] == "1"
        assert result["barcodes"][0]["barcode"] == "0012345"
        assert result["barcodes"][0]["container_id"] == str(UUID(int=10))
        assert result["type"] == "FUTURE_TYPE"


@pytest.mark.parametrize(
    "body",
    [b"", b"{}", b"[", b"[null]", b"[] trailing", b"[{}]", b"[]{}"],
)
def test_invalid_response_keeps_previous_snapshot(tmp_path: Path, body: bytes) -> None:
    source = Source()
    with TestClient(application(source, tmp_path)) as client:
        previous = client.post(f"{BASE}/load").json()
        source.body = body
        response = client.post(f"{BASE}/load")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_products_invalid_response"
        assert client.get(BASE).json()["snapshot"] == previous
        assert len(list(tmp_path.glob("*.json"))) == 2  # raw + current
        assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("bad_field", ["duplicate_id", "deleted", "numeric_num", "bad_uuid"])
def test_semantically_invalid_products_fail_whole_load(tmp_path: Path, bad_field: str) -> None:
    item = product()
    items = [item]
    if bad_field == "duplicate_id":
        items.append(product())
    elif bad_field == "deleted":
        item["deleted"] = True
    elif bad_field == "numeric_num":
        item["num"] = 12
    else:
        item["mainUnit"] = PASSWORD
    with TestClient(application(Source(json.dumps(items).encode()), tmp_path)) as client:
        response = client.post(f"{BASE}/load")
        assert response.status_code == 502
        assert PASSWORD not in response.text
        assert client.get(BASE).status_code == 409


def test_empty_catalog_is_a_valid_snapshot(tmp_path: Path) -> None:
    with TestClient(application(Source(b"[]"), tmp_path)) as client:
        assert client.post(f"{BASE}/load").json()["total"] == 0
        assert client.get(BASE).json()["items"] == []


@pytest.mark.parametrize("params", [{"offset": -1}, {"limit": 0}, {"limit": 201}])
def test_pagination_validation_does_not_access_iiko(tmp_path: Path, params: dict) -> None:
    source = Source()
    with TestClient(application(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 422
        assert source.paths == []


def test_stale_page_and_changed_source_are_detected(tmp_path: Path) -> None:
    source = Source()
    with TestClient(application(source, tmp_path)) as client:
        old = client.post(f"{BASE}/load").json()["snapshot_id"]
        assert client.post(f"{BASE}/load").status_code == 200
        assert client.get(BASE, params={"snapshot_id": old}).status_code == 409
    changed = settings().model_copy(update={"iiko_login": "different-account"})
    with TestClient(application(source, tmp_path, changed)) as client:
        response = client.get(BASE)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "iiko_products_source_changed"


def test_corrupt_local_metadata_has_safe_error(tmp_path: Path) -> None:
    (tmp_path / "current.json").write_text(PASSWORD)
    with TestClient(application(Source(), tmp_path)) as client:
        response = client.get(BASE)
        assert response.status_code == 500
        assert PASSWORD not in response.text


def test_changed_raw_file_is_detected_on_restart(tmp_path: Path) -> None:
    source = Source()
    with TestClient(application(source, tmp_path)) as client:
        snapshot = client.post(f"{BASE}/load").json()
    raw = tmp_path / f"{snapshot['snapshot_id']}.json"
    # Сохраняем длину и валидность JSON, но меняем содержание.
    raw.write_bytes(raw.read_bytes().replace(b"00012", b"00013"))
    with TestClient(application(source, tmp_path)) as client:
        assert client.get(BASE).status_code == 500


def test_failed_publication_preserves_previous_snapshot(tmp_path: Path, monkeypatch) -> None:
    source = Source()
    with TestClient(application(source, tmp_path)) as client:
        previous = client.post(f"{BASE}/load").json()
        original_replace = Path.replace

        def fail_pointer(path: Path, target: Path):
            if target.name == "current.json":
                raise PermissionError(PASSWORD)
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_pointer)
        response = client.post(f"{BASE}/load")
        assert response.status_code == 500
        assert PASSWORD not in response.text
        assert client.get(BASE).json()["snapshot"] == previous
    with TestClient(application(source, tmp_path)) as client:
        assert client.get(BASE).json()["snapshot"] == previous


def test_size_limit_removes_partial_download_and_preserves_session(tmp_path: Path) -> None:
    source = Source(b"[" + b" " * 100 + b"]")
    with TestClient(
        application(source, tmp_path, settings(iiko_products_max_response_bytes=16))
    ) as client:
        response = client.post(f"{BASE}/load")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_products_too_large"
        assert not list(tmp_path.iterdir())
        assert client.get("/api/v1/iiko/status").json()["state"] == "token_cached"
        assert client.post("/api/v1/iiko/logout").status_code == 200


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [429], [500]])
def test_only_401_renews_session_once(tmp_path: Path, statuses: list[int]) -> None:
    paths: list[str] = []
    remaining = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.rsplit("/", 1)[-1])
        if paths[-1] in {"auth", "logout"}:
            return httpx.Response(200, text=TOKEN)
        code = next(remaining)
        return httpx.Response(code, content=b"[]" if code == 200 else PASSWORD.encode())

    app = create_app(
        settings(), iiko_transport=httpx.MockTransport(handler), products_directory=tmp_path
    )
    with TestClient(app) as client:
        response = client.post(f"{BASE}/load")
        assert response.status_code == (200 if statuses[-1] == 200 else 502)
        assert PASSWORD not in response.text
        assert paths == (
            ["auth", "list", "auth", "list"] if statuses[0] == 401 else ["auth", "list"]
        )
        state = client.get("/api/v1/iiko/status").json()["state"]
        assert state == ("logged_out" if statuses[-1] == 401 else "token_cached")


def test_streaming_download_blocks_logout_until_body_is_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        body_started = asyncio.Event()
        release_body = asyncio.Event()
        paths: list[str] = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                body_started.set()
                yield b"["
                await release_body.wait()
                yield b"]"

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path.rsplit("/", 1)[-1])
            return (
                httpx.Response(200, stream=Stream())
                if paths[-1] == "list"
                else httpx.Response(200, text=TOKEN)
            )

        config = settings()
        auth = IikoAuthService(config, IikoClient(config, httpx.MockTransport(handler)))
        service = IikoProductsService(config, auth, tmp_path)
        try:
            load = asyncio.create_task(service.load())
            await body_started.wait()
            logout = asyncio.create_task(auth.logout())
            await asyncio.sleep(0)
            assert paths == ["auth", "list"]
            release_body.set()
            result, _ = await asyncio.gather(load, logout)
            assert result.total == 0
            assert paths == ["auth", "list", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_download_cleans_file_and_can_logout(tmp_path: Path, cancel: bool) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                yield b" " * 65536
                await asyncio.Event().wait()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/list"):
                return httpx.Response(200, stream=Stream())
            return httpx.Response(200, text=TOKEN)

        config = settings(iiko_products_timeout_seconds=0.05)
        auth = IikoAuthService(config, IikoClient(config, httpx.MockTransport(handler)))
        service = IikoProductsService(config, auth, tmp_path)
        try:
            task = asyncio.create_task(service.load())
            await started.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(IikoError) as failure:
                    await task
                assert failure.value.code == "iiko_timeout"
            assert auth.status().state == "token_cached"
            assert (await auth.logout()).state == "logged_out"
        finally:
            await auth.aclose()

    asyncio.run(scenario())
    assert not list(tmp_path.iterdir())


def test_products_request_logs_do_not_leak_secrets(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    with TestClient(application(Source(), tmp_path)) as client:
        assert client.post(f"{BASE}/load").status_code == 200
    assert TOKEN not in caplog.text
    assert PASSWORD not in caplog.text
    assert hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest() not in caplog.text
