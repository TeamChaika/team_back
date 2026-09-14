import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.main import create_app
from app.services.iiko_auth import IikoAuthService

TOKEN = "store-balance-test-token"
STORE = str(UUID(int=1))
PRODUCT = str(UUID(int=2))
DEPARTMENT = str(UUID(int=3))
BASE = "/api/v1/iiko/reports/store-balances"
SOURCE_PATH = "/resto/api/v2/reports/balance/stores"
PARAMS = {"timestamp": "2026-09-09T23:59:59"}


def row(**overrides):
    return {
        "store": STORE,
        "product": PRODUCT,
        "amount": "-0.123456789123456789",
        "sum": "-123.123456789123456789",
        **overrides,
    }


def body(rows=None):
    return (
        json.dumps([row()] if rows is None else rows)
        .replace('"-0.123456789123456789"', "-0.123456789123456789")
        .replace('"-123.123456789123456789"', "-123.123456789123456789")
        .encode()
    )


def config(**overrides):
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="api-test",
        iiko_password="test-secret",
        **overrides,
    )


class Source:
    def __init__(self, payload=None, statuses=None):
        self.body = body() if payload is None else payload
        self.statuses = iter(statuses or [200] * 10)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == SOURCE_PATH
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        store_balances_directory=directory,
    )


def test_precision_sign_zero_and_raw_are_preserved(tmp_path):
    source = Source(
        body([row(extraField={"keep": "raw"}), row(amount=0, sum=0), row(amount=2, sum=9)])
    )
    with TestClient(app(source, tmp_path)) as client:
        assert not source.requests
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert [r["amount"] for r in data["items"]] == ["-0.123456789123456789", "0", "2"]
        assert [r["sum"] for r in data["items"]] == ["-123.123456789123456789", "0", "9"]
        assert data["items"][0]["store_id"] == STORE
        assert data["items"][0]["product_id"] == PRODUCT
        assert "department_id" not in data["items"][0]
        assert "amount" not in data and "sum" not in data
        assert data["request"]["timestamp"] == PARAMS["timestamp"]
        assert dict(source.requests[1].url.params) == PARAMS
        raw = tmp_path / f"{data['snapshot_id']}.json"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert data["sha256"] == hashlib.sha256(source.body).hexdigest()
        assert data["source_bytes"] == len(source.body)
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        metadata = json.loads(meta.read_text())
        assert metadata["request"] == data["request"]
        assert metadata["source_endpoint"] == "v2/reports/balance/stores"
        assert TOKEN not in response.text and TOKEN not in meta.read_text()
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_repeated_filters_and_department_without_response_field(tmp_path):
    source = Source()
    params = {
        **PARAMS,
        "store_id": [STORE, str(UUID(int=11))],
        "product_id": [PRODUCT, str(UUID(int=12))],
        "department_id": [DEPARTMENT, str(UUID(int=13))],
    }
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 200
        sent = source.requests[1].url.params
        for key in ["store", "product", "department"]:
            assert sent.get_list(key) == params[key + "_id"]
        assert set(sent) == {"timestamp", "store", "product", "department"}
        assert sent["timestamp"] == PARAMS["timestamp"]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"timestamp": "2026-09-09"},
        {"timestamp": "1728000000"},
        {"timestamp": "2026-09-09T23:59:59Z"},
        {"timestamp": "2026-09-09T23:59:59+03:00"},
        {"timestamp": "2026-09-09T23:59:59.001"},
        {"timestamp": "2026-02-30T23:59:59"},
        {"timestamp": "2026-09-09 23:59:59"},
        {**PARAMS, "store_id": "bad"},
        {**PARAMS, "product_id": "bad"},
        {**PARAMS, "department_id": "bad"},
        *[{**PARAMS, field: [STORE] * 21} for field in ["store_id", "product_id", "department_id"]],
        {**PARAMS, "report_type": "egais"},
    ],
)
def test_invalid_input_does_not_start_an_iiko_session(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 422
        assert not source.requests


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[",
        b"null",
        b"[null]",
        b"private-error",
        b"[] extra",
        body([row(store="bad")]),
        body([row(product=None)]),
        body([row(amount="NaN")]),
        body([row(sum="Infinity")]),
        body([row(amount=None)]),
        body([row(sum=True)]),
        body([{"store": STORE, "product": PRODUCT, "sum": 1}]),
        body([row()]).replace(b'"amount":', b'"amount": 100, "amount":'),
    ],
)
def test_invalid_response_does_not_publish_or_leak(tmp_path, payload):
    with TestClient(app(Source(payload), tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_store_balances_invalid_response"
        assert "private-error" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", ["store_id", "product_id"])
def test_response_must_match_requested_filter(tmp_path, field):
    with TestClient(app(Source(), tmp_path)) as client:
        response = client.get(BASE, params={**PARAMS, field: str(UUID(int=100))})
        assert response.status_code == 502 and not list(tmp_path.iterdir())


def test_empty_and_repeated_rows_are_not_aggregated(tmp_path):
    source = Source(body([]))
    with TestClient(app(source, tmp_path)) as client:
        data = client.get(BASE, params=PARAMS).json()
        assert data["total"] == 0 and data["items"] == []
        source.body = body([row(), row()])
        data = client.get(BASE, params=PARAMS).json()
        assert data["total"] == 2 and data["items"][0] == data["items"][1]


def test_new_snapshot_on_each_request_and_failure_keeps_previous(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        first = client.get(BASE, params=PARAMS).json()
        second = client.get(BASE, params=PARAMS).json()
        assert first["snapshot_id"] != second["snapshot_id"]
        assert len(source.requests) == 3
        source.body = b"broken"
        assert client.get(BASE, params=PARAMS).status_code == 502
        assert len(list(tmp_path.glob("*.json"))) == 4


def test_limit_and_disk_failure_remove_unfinished_files(tmp_path, monkeypatch):
    settings = config(iiko_store_balances_max_response_bytes=10)
    with TestClient(app(Source(), tmp_path, settings)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.json()["error"]["code"] == "iiko_store_balances_too_large"
        assert not list(tmp_path.iterdir())

    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 500 and "private-error" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_reauthentication_is_bounded_and_only_after_401(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=PARAMS).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path == SOURCE_PATH for r in source.requests) == len(statuses)


def test_both_balance_reports_and_logout_share_one_request_lock(tmp_path):
    async def scenario():
        active = maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"[]"
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
                auth.download_store_balances(
                    tmp_path / "stock.json",
                    timestamp=datetime(2026, 9, 9, 23, 59, 59),
                    store_ids=[],
                    product_ids=[],
                    department_ids=[],
                ),
                auth.download_counteragent_balances(
                    tmp_path / "money.json",
                    timestamp=datetime(2026, 9, 9, 23, 59, 59),
                    account_ids=[],
                    counteragent_ids=[],
                    department_ids=[],
                ),
                auth.logout(),
            )
            assert maximum == 1 and paths == ["auth", "stores", "counteragents", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_timeout_keeps_session_available_for_logout(tmp_path):
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        if request.url.path == SOURCE_PATH:
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(
        app(handler, tmp_path, config(iiko_store_balances_timeout_seconds=0.01))
    ) as client:
        assert client.get(BASE, params=PARAMS).status_code == 504
        assert sum(path.endswith("/auth") for path in requests) == 1
        assert not list(tmp_path.iterdir())
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_openapi_has_stock_fields_filters_and_no_egais(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        schema = client.get("/openapi.json").json()
        fields = schema["components"]["schemas"]["StoreBalance"]["properties"]
        assert set(fields) == {"store_id", "product_id", "amount", "sum"}
        params = schema["paths"][BASE]["get"]["parameters"]
        assert {p["name"] for p in params} == {
            "timestamp",
            "department_id",
            "store_id",
            "product_id",
        }
        assert not any("egais" in path.lower() for path in schema["paths"])
        assert not source.requests
