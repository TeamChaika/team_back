import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

BASE = "/api/v1/iiko/olap/columns"
TOKEN = "olap-columns-test-token"


def column(**overrides):
    return {
        "name": "Сумма",
        "type": "MONEY",
        "aggregationAllowed": True,
        "groupingAllowed": False,
        "filteringAllowed": False,
        "tags": ["Оплата"],
        **overrides,
    }


def body(columns=None):
    return json.dumps({"Test.Amount": column()} if columns is None else columns).encode()


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
        assert request.url.host == "iiko.example"
        assert request.url.path == "/resto/api/v2/reports/olap/columns"
        assert dict(request.url.params) == {"reportType": "SALES"}
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        olap_columns_directory=directory,
    )


def test_preserves_query_keys_types_labels_flags_and_raw(tmp_path):
    source = Source(
        body(
            {
                "Test.Amount": column(extraField="retained in raw"),
                "Test.AmountOther": column(type="FUTURE_TYPE", tags=[]),
                "Test.Group": column(
                    name="Подразделение",
                    type="ID",
                    aggregationAllowed=False,
                    groupingAllowed=True,
                    filteringAllowed=True,
                ),
            }
        )
    )
    with TestClient(app(source, tmp_path)) as c:
        r = c.get(BASE, params={"report_type": "SALES"})
        assert r.status_code == 200
        data = r.json()
        assert data["connection_id"] == "primary" and data["request"] == {"report_type": "SALES"}
        assert data["total"] == 3
        assert set(data["columns"]) == {"Test.Amount", "Test.AmountOther", "Test.Group"}
        assert data["columns"]["Test.Amount"] == {
            "name": "Сумма",
            "type": "MONEY",
            "aggregation_allowed": True,
            "grouping_allowed": False,
            "filtering_allowed": False,
            "tags": ["Оплата"],
        }
        assert data["columns"]["Test.AmountOther"]["type"] == "FUTURE_TYPE"
        assert data["columns"]["Test.AmountOther"]["name"] == "Сумма"
        assert data["columns"]["Test.Group"]["grouping_allowed"] is True
        assert data["columns"]["Test.Group"]["aggregation_allowed"] is False
        raw = tmp_path / f"{data['snapshot_id']}.json"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert data["source_bytes"] == len(source.body)
        assert data["sha256"] == hashlib.sha256(source.body).hexdigest()
        metadata = json.loads(meta.read_bytes())
        assert metadata["request"] == {"report_type": "SALES"}
        assert metadata["source_endpoint"] == "v2/reports/olap/columns"
        assert (
            metadata["source_fingerprint"]
            == hashlib.sha256(b"https://iiko.example/resto/api\napi-test").hexdigest()
        )
        assert "columns" not in metadata
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert TOKEN not in r.text and TOKEN not in meta.read_text()


@pytest.mark.parametrize(
    "params",
    [
        {"report_type": "TRANSACTIONS"},
        {"report_type": "DELIVERIES"},
        {"report_type": "egais"},
        {"report_type": "sales"},
        {"report_type": ""},
        {"reportType": "SALES"},
        {"connection_id": "another-rms"},
        {"url": "https://untrusted.example"},
    ],
)
def test_invalid_query_does_not_call_iiko(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=params).status_code == 422 and not source.requests


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"null",
        b"[",
        b"private-error",
        b"{} extra",
        b"\xff",
        body({"": column()}),
        body({"Field": None}),
        body({"Field": column(name="")}),
        body({"Field": column(type="")}),
        body({"Field": column(tags=None)}),
        body({"Field": column(tags=[1])}),
        body({"Field": column(aggregationAllowed="false")}),
        body({"Field": column(groupingAllowed=1)}),
        body({"Field": column(filteringAllowed=None)}),
        body().replace(b'"aggregationAllowed"', b'"aggregationTypo"'),
        body().replace(b'"type":', b'"type": "private-error", "type":'),
        b'{"Test.Amount": {}, "Test.Amount": {}}',
    ],
)
def test_invalid_source_is_not_published_or_leaked(tmp_path, payload):
    with TestClient(app(Source(payload), tmp_path)) as c:
        r = c.get(BASE)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "iiko_olap_columns_invalid_response"
        assert "private-error" not in r.text and not list(tmp_path.iterdir())


def test_default_sales_empty_response_and_append_only_snapshots(tmp_path):
    source = Source(b"{}")
    with TestClient(app(source, tmp_path)) as c:
        first = c.get(BASE).json()
        assert first["columns"] == {} and first["total"] == 0
        source.body = body()
        second = c.get(BASE).json()
        assert second["total"] == 1 and first["snapshot_id"] != second["snapshot_id"]
        source.body = b"broken"
        assert c.get(BASE).status_code == 502
        assert len(list(tmp_path.glob("*.json"))) == 4
        assert (tmp_path / f"{first['snapshot_id']}.json").read_bytes() == b"{}"
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"
        assert sum(r.url.path.endswith("/auth") for r in source.requests) == 1


def test_size_and_disk_failure_clean_unfinished_files(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_olap_columns_max_response_bytes=10))) as c:
        r = c.get(BASE)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "iiko_olap_columns_too_large"
        assert not list(tmp_path.iterdir())

    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as c:
        r = c.get(BASE)
        assert r.status_code == 500 and "private-error" not in r.text
        assert r.json()["error"]["code"] == "iiko_olap_columns_storage_error"
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_only_401_is_retried_once(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/columns") for r in source.requests) == len(statuses)
        assert sum(r.url.path.endswith("/auth") for r in source.requests) == (
            2 if statuses[0] == 401 else 1
        )


def test_timeout_allows_logout(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/columns"):
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_olap_columns_timeout_seconds=0.01))) as c:
        assert c.get(BASE).status_code == 504
        assert not list(tmp_path.iterdir())
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_olap_and_named_primary_share_session_and_full_stream_lock(tmp_path):
    async def scenario():
        active = maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            def __init__(self, data):
                self.data = data

            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield self.data
                active -= 1

        def handler(request):
            nonlocal active, maximum
            path = request.url.path.rsplit("/", 1)[-1]
            paths.append(path)
            if path in {"auth", "logout"}:
                assert active == 0
                return httpx.Response(200, text=TOKEN)
            assert request.headers["cookie"] == f"key={TOKEN}"
            active += 1
            maximum = max(active, maximum)
            return httpx.Response(200, stream=Stream(b"{}" if path == "columns" else b'"CHAIN"'))

        application = create_app(
            config(),
            iiko_transport=httpx.MockTransport(handler),
            olap_columns_directory=tmp_path / "columns",
            connections_directory=tmp_path / "connections",
        )
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as c:
                responses = await asyncio.gather(
                    c.get(BASE),
                    c.get("/api/v1/iiko/connections/primary/server-type"),
                )
                assert all(r.status_code == 200 for r in responses)
                assert maximum == 1 and paths.count("auth") == 1
                assert (await c.post("/api/v1/iiko/logout")).json()["state"] == "logged_out"
                assert paths[-1] == "logout"

    asyncio.run(scenario())


def test_openapi_exposes_only_sales_columns_and_public_metadata(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        schema = c.get("/openapi.json").json()
        assert {p for p in schema["paths"] if p.startswith(BASE)} == {BASE}
        assert set(schema["paths"][BASE]) == {"get"}
        params = schema["paths"][BASE]["get"]["parameters"]
        assert len(params) == 1 and params[0]["name"] == "report_type"
        assert params[0]["schema"]["const"] == "SALES"
        fields = schema["components"]["schemas"]["OlapColumn"]["properties"]
        assert "aggregation_allowed" in fields and "aggregationAllowed" not in fields
        assert not any("egais" in p.lower() for p in schema["paths"]) and not source.requests
