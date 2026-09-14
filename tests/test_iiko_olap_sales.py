import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

BASE = "/api/v1/iiko/olap/sales/daily"
PARAMS = {"business_date": "2026-09-09"}
TOKEN = "daily-sales-test-token"


def row(department=1, **overrides):
    return {
        "OpenDate.Typed": "2026-09-09",
        "Department.Id": str(UUID(int=department)),
        "Department": "Ресторан",
        "DishDiscountSumInt": "100.123456789123456789",
        "ProductCostBase.ProductCost": "20.123456789123456789",
        "UniqOrderId": 2,
        "GuestNum": "3.5",
        **overrides,
    }


def body(rows=None):
    return (
        json.dumps({"data": [row()] if rows is None else rows, "summary": []})
        .replace('"100.123456789123456789"', "100.123456789123456789")
        .replace('"20.123456789123456789"', "20.123456789123456789")
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
            assert request.method == "GET" and not request.content
            return httpx.Response(200, text=TOKEN)
        assert request.method == "POST" and request.url.path == "/resto/api/v2/reports/olap"
        assert not request.url.query
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        olap_sales_directory=directory,
    )


def test_daily_request_precision_nulls_source_identity_and_raw(tmp_path):
    source = Source(
        body(
            [
                row(extraField="kept in raw"),
                row(
                    2,
                    **{
                        "DishDiscountSumInt": -5,
                        "ProductCostBase.ProductCost": None,
                        "GuestNum": None,
                    },
                ),
            ]
        )
    )
    with TestClient(app(source, tmp_path)) as c:
        response = c.get(BASE, params=PARAMS)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2 and data["connection_id"] == "primary"
        first, second = data["items"]
        assert first["revenue"] == "100.123456789123456789"
        assert first["cost"] == "20.123456789123456789"
        assert first["checks"] == 2 and first["guests"] == "3.5"
        assert second["revenue"] == "-5" and second["cost"] is None and second["guests"] is None
        assert first["department_name"] == second["department_name"] == "Ресторан"
        assert first["department_id"] != second["department_id"]
        raw = tmp_path / f"{data['snapshot_id']}.json"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert data["sha256"] == hashlib.sha256(source.body).hexdigest()
        assert data["source_bytes"] == len(source.body)
        metadata = json.loads(meta.read_bytes())
        request_body = json.loads(source.requests[1].content)
        assert metadata["source_method"] == "POST" and metadata["source_request"] == request_body
        assert (
            metadata["source_fingerprint"]
            == hashlib.sha256(b"https://iiko.example/resto/api\napi-test").hexdigest()
        )
        assert request_body["reportType"] == "SALES" and request_body["buildSummary"] is False
        assert request_body["groupByRowFields"] == ["OpenDate.Typed", "Department.Id", "Department"]
        assert request_body["aggregateFields"] == [
            "DishDiscountSumInt",
            "ProductCostBase.ProductCost",
            "UniqOrderId",
            "GuestNum",
        ]
        assert request_body["groupByColFields"] == []
        assert request_body["filters"] == {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "periodType": "CUSTOM",
                "from": "2026-09-09T00:00:00.000",
                "to": "2026-09-10T00:00:00.000",
                "includeLow": True,
                "includeHigh": False,
            },
            "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
            "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
        }
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert TOKEN not in response.text and TOKEN not in meta.read_text()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"business_date": "2026-02-30"},
        {"business_date": "2026-09-09T00:00:00"},
        {"business_date": "1728000000"},
        {"business_date": "9999-12-31"},
        {**PARAMS, "date_to": "2026-09-10"},
        {**PARAMS, "build_summary": True},
        {**PARAMS, "report_type": "TRANSACTIONS"},
        {**PARAMS, "connection_id": "rms"},
    ],
)
def test_invalid_params_do_not_open_session(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=params).status_code == 422 and not source.requests


@pytest.mark.parametrize(
    "day, following",
    [
        ("2026-09-30", "2026-10-01"),
        ("2026-12-31", "2027-01-01"),
        ("2024-02-29", "2024-03-01"),
    ],
)
def test_exact_one_day_across_calendar_boundaries(tmp_path, day, following):
    source = Source(body([]))
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params={"business_date": day}).status_code == 200
        period = json.loads(source.requests[1].content)["filters"]["OpenDate.Typed"]
        assert period["from"] == day + "T00:00:00.000"
        assert period["to"] == following + "T00:00:00.000" and period["includeHigh"] is False


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"null",
        b"{}",
        b"private-error",
        b'{"data":null,"summary":[]}',
        b'{"data":[],"summary":[[{},{}]]}',
        body([row(**{"OpenDate.Typed": "2026-09-10"})]),
        body([row(**{"OpenDate.Typed": 1728000000})]),
        body([row(**{"Department.Id": "bad"})]),
        body([row(), row()]),
        body([row(**{"DishDiscountSumInt": "NaN"})]),
        body([row(**{"ProductCostBase.ProductCost": "Infinity"})]),
        body([row(**{"GuestNum": True})]),
        body([row(**{"UniqOrderId": 1.5})]),
        body([row(**{"UniqOrderId": True})]),
        body().replace(b'"ProductCostBase.ProductCost"', b'"CostTypo"'),
        body().replace(b'"data":', b'"data": "private-error", "data":'),
    ],
)
def test_invalid_response_does_not_publish(tmp_path, payload):
    with TestClient(app(Source(payload), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "iiko_olap_sales_invalid_response"
        assert "private-error" not in r.text and not list(tmp_path.iterdir())


def test_empty_report_is_not_filled_with_zero_restaurants_and_snapshots_append(tmp_path):
    source = Source(body([]))
    with TestClient(app(source, tmp_path)) as c:
        first = c.get(BASE, params=PARAMS).json()
        assert first["items"] == [] and first["total"] == 0
        source.body = body()
        second = c.get(BASE, params=PARAMS).json()
        assert second["snapshot_id"] != first["snapshot_id"] and second["total"] == 1
        assert len(list(tmp_path.glob("*.json"))) == 4
        assert sum(r.url.path.endswith("/auth") for r in source.requests) == 1
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_post_read_only_retry_is_bounded_to_one_401(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=PARAMS).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.method == "POST" for r in source.requests) == len(statuses)


def test_size_and_disk_failure_cleanup(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_olap_sales_max_response_bytes=10))) as c:
        assert c.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_olap_sales_too_large"
        assert not list(tmp_path.iterdir())

    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 500 and "private-error" not in r.text
        assert not list(tmp_path.iterdir())


def test_timeout_does_not_retry_report_and_releases_session(tmp_path):
    reports = []

    async def handler(request):
        if request.method == "POST":
            reports.append(request)
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_olap_sales_timeout_seconds=0.01))) as c:
        assert c.get(BASE, params=PARAMS).status_code == 504 and len(reports) == 1
        assert not list(tmp_path.iterdir())
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_sales_post_and_columns_get_share_full_stream_lock(tmp_path):
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
            assert request.method == ("POST" if path == "olap" else "GET")
            active += 1
            maximum = max(active, maximum)
            return httpx.Response(200, stream=Stream(body() if path == "olap" else b"{}"))

        application = create_app(
            config(),
            iiko_transport=httpx.MockTransport(handler),
            olap_sales_directory=tmp_path / "sales",
            olap_columns_directory=tmp_path / "columns",
        )
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as c:
                responses = await asyncio.gather(
                    c.get(BASE, params=PARAMS),
                    c.get("/api/v1/iiko/olap/columns"),
                )
                assert all(r.status_code == 200 for r in responses)
                assert maximum == 1 and paths.count("auth") == 1
                assert (await c.post("/api/v1/iiko/logout")).json()["state"] == "logged_out"

    asyncio.run(scenario())
