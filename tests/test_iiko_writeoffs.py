import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.iiko.client import IikoClient
from app.main import create_app
from app.services.iiko_auth import IikoAuthService

TOKEN = "writeoff-test-token"
BASE = "/api/v1/iiko/writeoffs"
PARAMS = {"date_from": "2026-09-09", "date_to": "2026-09-09"}
PRODUCT, STORE, ACCOUNT, UNIT = [str(UUID(int=n)) for n in range(100, 104)]


def item(**overrides):
    return {
        "num": 1,
        "productId": PRODUCT,
        "amount": "-1.23456789123456789",
        "amountFactor": "0.750000000",
        "cost": "101.123456789123456789",
        "measureUnitId": UNIT,
        "containerId": None,
        "productSizeId": None,
        **overrides,
    }


def document(doc_id=1, **overrides):
    return {
        "id": str(UUID(int=doc_id)),
        "documentNumber": "00007",
        "dateIncoming": "2026-09-09T23:00",
        "status": "PROCESSED",
        "storeId": STORE,
        "accountId": ACCOUNT,
        "comment": "null",
        "items": [item()],
        **overrides,
    }


def body(documents=None, **overrides):
    return (
        json.dumps(
            {
                "result": "SUCCESS",
                "errors": [],
                "revision": 987654,
                "response": [document()] if documents is None else documents,
                **overrides,
            }
        )
        .replace('"-1.23456789123456789"', "-1.23456789123456789")
        .replace('"101.123456789123456789"', "101.123456789123456789")
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
        assert request.url.path == "/resto/api/v2/documents/writeoff"
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        writeoffs_directory=directory,
    )


def test_precision_cost_nulls_statuses_dates_and_revision_are_preserved(tmp_path):
    source = Source(
        body(
            [
                document(extraField={"keep": "raw"}),
                document(2, status="NEW", dateIncoming="2026-09-09T00:00", items=[item(cost=None)]),
                document(3, status="DELETED", items=[]),
            ]
        )
    )
    with TestClient(app(source, tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3 and data["items_count"] == 2 and data["revision"] == 987654
        assert [d["status"] for d in data["documents"]] == ["PROCESSED", "NEW", "DELETED"]
        first, second = data["documents"][:2]
        assert first["document_number"] == "00007" and first["comment"] == "null"
        assert first["date_incoming"] == "2026-09-09T23:00:00"
        assert second["date_incoming"] == "2026-09-09T00:00:00"
        line = first["items"][0]
        assert line["amount"] == "-1.23456789123456789" and line["amount_factor"] == "0.750000000"
        assert line["cost"] == "101.123456789123456789" and second["items"][0]["cost"] is None
        assert line["measure_unit_id"] == UNIT and line["container_id"] is None
        assert "revision" not in first and "cost" not in data
        assert dict(source.requests[1].url.params) == {
            "dateFrom": "2026-09-09",
            "dateTo": "2026-09-09",
            "revisionFrom": "-1",
        }
        raw = tmp_path / f"{data['snapshot_id']}.json"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert (
            data["source_bytes"] == len(source.body)
            and data["sha256"] == hashlib.sha256(source.body).hexdigest()
        )
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        metadata = json.loads(meta.read_text())
        assert metadata["request"] == data["request"] and metadata["revision"] == 987654
        assert metadata["source_endpoint"] == "v2/documents/writeoff"
        assert TOKEN not in r.text and TOKEN not in meta.read_text()
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_status_revision_and_seven_day_boundary(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        r = c.get(
            BASE,
            params={
                **PARAMS,
                "date_from": "2026-09-03",
                "status": "PROCESSED",
                "revision_from": 123,
            },
        )
        assert r.status_code == 200
        assert dict(source.requests[1].url.params) == {
            "dateFrom": "2026-09-03",
            "dateTo": "2026-09-09",
            "status": "PROCESSED",
            "revisionFrom": "123",
        }


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"date_from": "2026-09-09"},
        {**PARAMS, "date_to": "2026-09-08"},
        {**PARAMS, "date_to": "2026-09-16"},
        {**PARAMS, "date_from": "2026-02-30"},
        {**PARAMS, "date_from": "1728000000"},
        {**PARAMS, "date_from": "2026-09-09T00:00:00Z"},
        {**PARAMS, "status": "INVALID"},
        {**PARAMS, "revision_from": -2},
        {**PARAMS, "revision_from": "1.1"},
        {**PARAMS, "store_id": STORE},
        {**PARAMS, "report_type": "egais"},
    ],
)
def test_invalid_params_do_not_start_session(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=params).status_code == 422 and not source.requests


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"[",
        b"private-error",
        b"null",
        b"[] extra",
        body(response=None),
        body(revision=True),
        body(revision="123"),
        body(revision=None),
        body([document(id="bad")]),
        body([document(items=None)]),
        body([document(items=[item(amount="NaN")])]),
        body([document(items=[item(cost="Infinity")])]),
        body([document(items=[item(num=True)])]),
        body([document(items=[item(productId=None)])]),
        body([document(items=[item(), item()])]),
        body([document(), document()]),
        body([document(dateIncoming="2026-09-08T23:59")]),
        body([document(dateIncoming="2026-09-10T00:00")]),
        body([document(dateIncoming="2026-09-09")]),
        body([document(dateIncoming="2026-09-09T00:00Z")]),
        body().replace(b'"revision":', b'"revision": 1, "revision":'),
    ],
)
def test_invalid_exports_are_not_published_or_leaked(tmp_path, payload):
    with TestClient(app(Source(payload), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert (
            r.status_code == 502 and r.json()["error"]["code"] == "iiko_writeoffs_invalid_response"
        )
        assert "private-error" not in r.text and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "overrides", [{"result": "ERROR"}, {"errors": [{"message": "private-error"}]}]
)
def test_http_200_business_errors_do_not_publish_revision(tmp_path, overrides):
    with TestClient(app(Source(body(**overrides)), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 502 and r.json()["error"]["code"] == "iiko_writeoffs_export_failed"
        assert "private-error" not in r.text and not list(tmp_path.iterdir())


def test_status_filter_must_match(tmp_path):
    with TestClient(app(Source(), tmp_path)) as c:
        assert c.get(BASE, params={**PARAMS, "status": "NEW"}).status_code == 502
        assert not list(tmp_path.iterdir())


def test_empty_export_retains_revision_and_new_requests_have_new_snapshots(tmp_path):
    source = Source(body([]))
    with TestClient(app(source, tmp_path)) as c:
        first = c.get(BASE, params=PARAMS).json()
        assert first["documents"] == [] and first["total"] == first["items_count"] == 0
        assert first["revision"] == 987654
        source.body = body([document(items=[item(), item(num=2)])])
        second = c.get(BASE, params=PARAMS).json()
        assert second["items_count"] == 2 and second["snapshot_id"] != first["snapshot_id"]
        source.body = b"broken"
        assert c.get(BASE, params=PARAMS).status_code == 502
        assert len(list(tmp_path.glob("*.json"))) == 4


def test_size_and_disk_failure_clean_unfinished_files(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_writeoffs_max_response_bytes=10))) as c:
        assert c.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_writeoffs_too_large"
        assert not list(tmp_path.iterdir())

    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert (
            r.status_code == 500 and "private-error" not in r.text and not list(tmp_path.iterdir())
        )


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_only_401_is_retried_once(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=PARAMS).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/writeoff") for r in source.requests) == len(statuses)


def test_timeout_keeps_session_available_for_logout(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/writeoff"):
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_writeoffs_timeout_seconds=0.01))) as c:
        assert c.get(BASE, params=PARAMS).status_code == 504
        assert not list(tmp_path.iterdir())
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_writeoffs_employees_and_logout_share_one_request_lock(tmp_path):
    async def scenario():
        active = maximum = 0
        paths = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"{}"
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
                auth.download_writeoffs(
                    tmp_path / "writeoffs.json",
                    date_from=date(2026, 9, 9),
                    date_to=date(2026, 9, 9),
                    status=None,
                    revision_from=-1,
                ),
                auth.download_employees(tmp_path / "employees.xml"),
                auth.logout(),
            )
            assert maximum == 1 and paths == ["auth", "writeoff", "employees", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_openapi_exposes_only_list_reading_and_public_field_names(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        schema = c.get("/openapi.json").json()
        assert {p for p in schema["paths"] if p.startswith(BASE)} == {BASE}
        assert set(schema["paths"][BASE]) == {"get"}
        fields = schema["components"]["schemas"]["WriteoffDocumentItem"]["properties"]
        assert (
            "product_id" in fields
            and "productId" not in fields
            and fields["cost"]["examples"] == ["123.45"]
        )
        assert not any("egais" in p.lower() for p in schema["paths"]) and not source.requests
