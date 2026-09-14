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

TOKEN = "balance-test-token"
ACCOUNT = str(UUID(int=1))
COUNTERAGENT = str(UUID(int=2))
DEPARTMENT = str(UUID(int=3))
BASE = "/api/v1/iiko/reports/counteragent-balances"
PARAMS = {"timestamp": "2026-09-09T23:59:59"}


def row(**overrides):
    return {
        "account": ACCOUNT,
        "counteragent": COUNTERAGENT,
        "department": DEPARTMENT,
        "sum": "-123.123456789123456789",
        **overrides,
    }


def body(rows=None):
    return (
        json.dumps([row()] if rows is None else rows)
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
        assert request.url.path == "/resto/api/v2/reports/balance/counteragents"
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        balances_directory=directory,
    )


def test_balance_precision_sign_nulls_and_raw_are_preserved(tmp_path):
    source = Source(
        body(
            [
                row(extraField={"keep": "raw"}),
                row(counteragent=None, department=None, sum=0),
                row(sum=20),
            ]
        )
    )
    with TestClient(app(source, tmp_path)) as client:
        assert not source.requests
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert [r["sum"] for r in data["items"]] == ["-123.123456789123456789", "0", "20"]
        assert data["items"][1]["counteragent_id"] is None
        assert data["items"][1]["department_id"] is None
        assert data["request"]["timestamp"] == PARAMS["timestamp"]
        assert "Z" not in data["request"]["timestamp"]
        assert dict(source.requests[1].url.params) == PARAMS
        assert "sum" not in {k for k in data if k != "items"}
        raw = tmp_path / f"{data['snapshot_id']}.json"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert data["sha256"] == hashlib.sha256(source.body).hexdigest()
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        assert json.loads(meta.read_text())["request"] == data["request"]
        assert TOKEN not in response.text and TOKEN not in meta.read_text()
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_multiple_filter_ids_are_repeated_query_parameters(tmp_path):
    source = Source()
    params = {
        **PARAMS,
        "account_id": [ACCOUNT, str(UUID(int=11))],
        "counteragent_id": [COUNTERAGENT, str(UUID(int=12))],
        "department_id": [DEPARTMENT, str(UUID(int=13))],
    }
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 200
        sent = source.requests[1].url.params
        for key in ["account", "counteragent", "department"]:
            assert sent.get_list(key) == params[key + "_id"]
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
        {**PARAMS, "account_id": "bad"},
        {**PARAMS, "counteragent_id": [COUNTERAGENT] * 21},
        {**PARAMS, "department_id": "bad"},
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
        body([row(account="bad")]),
        body([row(sum="NaN")]),
        body([row(sum="Infinity")]),
        body([{"account": ACCOUNT, "department": DEPARTMENT, "sum": 1}]),
        body([row()]).replace(b'"sum":', b'"sum": 100, "sum":'),
    ],
)
def test_invalid_response_does_not_publish_or_leak(tmp_path, payload):
    source = Source(payload)
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_balances_invalid_response"
        assert "private-error" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", ["account_id", "counteragent_id", "department_id"])
def test_response_must_match_requested_filter(tmp_path, field):
    with TestClient(app(Source(), tmp_path)) as client:
        response = client.get(BASE, params={**PARAMS, field: str(UUID(int=100))})
        assert response.status_code == 502 and not list(tmp_path.iterdir())


def test_empty_and_repeated_rows_are_not_changed(tmp_path):
    source = Source(body([]))
    with TestClient(app(source, tmp_path)) as client:
        data = client.get(BASE, params=PARAMS).json()
        assert data["total"] == 0 and data["items"] == []
        source.body = body([row(), row()])
        data = client.get(BASE, params=PARAMS).json()
        assert data["total"] == 2 and data["items"][0] == data["items"][1]


def test_new_request_creates_new_snapshot_and_failure_keeps_previous(tmp_path):
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
    with TestClient(app(Source(), tmp_path, config(iiko_balances_max_response_bytes=10))) as client:
        assert client.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_balances_too_large"
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
        assert sum(r.url.path.endswith("/counteragents") for r in source.requests) == len(statuses)


def test_balance_request_stores_and_logout_are_sequential(tmp_path):
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
                auth.download_counteragent_balances(
                    tmp_path / "balance.json",
                    timestamp=datetime(2026, 9, 9, 23, 59, 59),
                    account_ids=[],
                    counteragent_ids=[],
                    department_ids=[],
                ),
                auth.download_stores(tmp_path / "stores.xml"),
                auth.logout(),
            )
            assert maximum == 1 and paths == ["auth", "counteragents", "stores", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_timeout_does_not_request_another_token(tmp_path):
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/counteragents"):
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_balances_timeout_seconds=0.01))) as client:
        assert client.get(BASE, params=PARAMS).status_code == 504
        assert sum(path.endswith("/auth") for path in requests) == 1
        assert not list(tmp_path.iterdir())
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_balance_reports_are_explicit_and_openapi_uses_public_field_names(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        schema = client.get("/openapi.json").json()
        assert {path for path in schema["paths"] if "/reports/" in path} == {
            BASE,
            "/api/v1/iiko/reports/store-balances",
        }
        assert not any("egais" in path.lower() for path in schema["paths"])
        fields = schema["components"]["schemas"]["CounteragentBalance"]["properties"]
        assert set(fields) == {"account_id", "counteragent_id", "department_id", "sum"}
        assert not source.requests
