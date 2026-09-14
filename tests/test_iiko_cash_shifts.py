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

TOKEN = "cash-shift-test-token"
BASE = "/api/v1/iiko/cash-shifts"
PARAMS = {"open_date_from": "2026-09-09", "open_date_to": "2026-09-09"}
MANAGER, CASHIER, POS, DEPARTMENT, GROUP = [str(UUID(int=n)) for n in range(100, 105)]


def shift(shift_id=1, **overrides):
    return {
        "id": str(UUID(int=shift_id)),
        "sessionNumber": 7,
        "fiscalNumber": None,
        "cashRegNumber": 1,
        "cashRegSerial": "000001 ",
        "openDate": "2026-09-09T09:56:32.937",
        "closeDate": "2026-09-10T01:28:18.63",
        "acceptDate": None,
        "managerId": MANAGER,
        "responsibleUser": CASHIER,
        "sessionStartCash": 0,
        "payOrders": "101.123456789123456789",
        "sumWriteoffOrders": 0,
        "salesCash": "101.123456789123456789",
        "salesCredit": 0,
        "salesCard": 0,
        "payIn": 0,
        "payOut": 0,
        "payIncome": "-101.123456789123456789",
        "cashRemain": None,
        "cashDiff": "-101.123456789123456789",
        "sessionStatus": "UNACCEPTED",
        "conception": None,
        "pointOfSale": POS,
        **overrides,
    }


def body(shifts=None):
    return (
        json.dumps([shift()] if shifts is None else shifts)
        .replace('"101.123456789123456789"', "101.123456789123456789")
        .replace('"-101.123456789123456789"', "-101.123456789123456789")
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
        assert request.method == "GET" and request.url.path == "/resto/api/v2/cashshifts/list"
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        cash_shifts_directory=directory,
    )


def test_fields_precision_nulls_next_day_close_and_duplicate_numbers(tmp_path):
    source = Source(
        body(
            [
                shift(extraField="kept in raw"),
                shift(2, cashRegNumber=2, closeDate=None, cashRegSerial=None),
                shift(3, acceptDate="2026-09-11T12:13:14.123456", sessionStatus="HASWARNINGS"),
            ]
        )
    )
    with TestClient(app(source, tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3 and "revision" not in data
        first, second, third = data["items"]
        assert first["pay_orders"] == first["sales_cash"] == "101.123456789123456789"
        assert first["pay_income"] == first["cash_diff"] == "-101.123456789123456789"
        assert first["session_start_cash"] == "0" and first["cash_remain"] is None
        assert first["fiscal_number"] is None and first["cash_reg_serial"] == "000001 "
        assert first["open_date"] == "2026-09-09T09:56:32.937000"
        assert first["close_date"] == "2026-09-10T01:28:18.630000"
        assert first["accept_date"] is None and second["close_date"] is None
        assert third["accept_date"] == "2026-09-11T12:13:14.123456"
        assert first["manager_id"] == MANAGER and first["responsible_user_id"] == CASHIER
        assert first["point_of_sale_id"] == POS and first["conception_id"] is None
        assert first["session_status"] == "UNACCEPTED" and third["session_status"] == "HASWARNINGS"
        assert "extraField" not in first and "revision" not in first
        assert dict(source.requests[1].url.params) == {
            "openDateFrom": "2026-09-09",
            "openDateTo": "2026-09-09",
            "status": "ANY",
            "revisionFrom": "-1",
        }
        raw = tmp_path / f"{data['snapshot_id']}.json"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert data["sha256"] == hashlib.sha256(source.body).hexdigest()
        assert data["source_bytes"] == len(source.body)
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        metadata = json.loads(meta.read_text())
        assert metadata["request"] == data["request"]
        assert metadata["source_endpoint"] == "v2/cashshifts/list"
        assert TOKEN not in r.text and TOKEN not in meta.read_text()
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_dates_outside_request_are_preserved_with_raw_and_reported(tmp_path):
    source = Source(
        body(
            [
                shift(openDate="2026-09-08T23:25:14.297"),
                shift(2),
                shift(3, openDate="2026-09-10T00:00:00"),
            ]
        )
    )
    with TestClient(app(source, tmp_path)) as c:
        response = c.get(BASE, params=PARAMS)
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 3 and result["opening_dates_outside_request"] == 2
        assert result["items"][0]["open_date"] == "2026-09-08T23:25:14.297000"
        assert result["request"]["open_date_from"] == "2026-09-09"
        assert (tmp_path / f"{result['snapshot_id']}.json").read_bytes() == source.body


def test_closed_filter_is_not_compared_to_acceptance_status_and_repeated_filters(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        r = c.get(
            BASE,
            params={
                **PARAMS,
                "open_date_from": "2026-09-03",
                "status": "CLOSED",
                "revision_from": 123,
                "department_id": [DEPARTMENT, MANAGER],
                "group_id": [GROUP, CASHIER],
            },
        )
        assert r.status_code == 200 and r.json()["items"][0]["session_status"] == "UNACCEPTED"
        params = source.requests[1].url.params
        assert params.get_list("departmentId") == [DEPARTMENT, MANAGER]
        assert params.get_list("groupId") == [GROUP, CASHIER]
        assert params["status"] == "CLOSED" and params["revisionFrom"] == "123"
        assert params["openDateFrom"] == "2026-09-03"


def test_live_reference_names_are_supported_without_losing_nullable_ids(tmp_path):
    row = shift()
    for old, current in [
        ("responsibleUser", "responsibleUserId"),
        ("conception", "conceptionId"),
        ("pointOfSale", "pointOfSaleId"),
    ]:
        row[current] = row.pop(old)
    with TestClient(app(Source(body([row])), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert item["responsible_user_id"] == CASHIER and item["point_of_sale_id"] == POS
        assert item["conception_id"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"responsibleUserId": MANAGER},
        {"conceptionId": GROUP},
        {"pointOfSaleId": DEPARTMENT},
    ],
)
def test_conflicting_reference_names_are_rejected(tmp_path, extra):
    with TestClient(app(Source(body([shift(**extra)])), tmp_path)) as c:
        assert c.get(BASE, params=PARAMS).status_code == 502
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"open_date_from": "2026-09-09"},
        {**PARAMS, "open_date_to": "2026-09-08"},
        {**PARAMS, "open_date_to": "2026-09-16"},
        {**PARAMS, "open_date_from": "2026-02-30"},
        {**PARAMS, "open_date_from": "1728000000"},
        {**PARAMS, "open_date_from": "2026-09-09T00:00:00"},
        {**PARAMS, "status": ""},
        {**PARAMS, "status": "INVALID"},
        {**PARAMS, "revision_from": -2},
        {**PARAMS, "revision_from": "1.1"},
        {**PARAMS, "department_id": "bad"},
        {**PARAMS, "group_id": "bad"},
        {**PARAMS, "group_id": [GROUP] * 101},
        {**PARAMS, "date_from": "2026-09-09"},
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
        b"[",
        b"private-error",
        b"null",
        b"[] extra",
        b'{"result":"SUCCESS","response":[],"revision":123}',
        body([shift(id="bad")]),
        body([shift(), shift()]),
        body([shift(sessionNumber=True)]),
        body([shift(fiscalNumber="007")]),
        body([shift(payOrders="NaN")]),
        body([shift(salesCash="Infinity")]),
        body([shift(payIncome=True)]),
        body([shift(pointOfSale="bad")]),
        body([shift(openDate=None)]),
        body([shift(closeDate=1728000000)]),
        body([shift(openDate="2026-09-09")]),
        body([shift(acceptDate="2026-09-09T00:00:00Z")]),
        body([shift(closeDate="2026-09-09T00:00:00.1234567")]),
        body().replace(b'"salesCredit": 0', b'"salesCerdit": 0'),
        body().replace(b'"sessionStatus":', b'"sessionStaus":'),
        body().replace(b'"id":', b'"id": "private-error", "id":'),
    ],
)
def test_invalid_source_is_not_published_or_leaked(tmp_path, payload):
    with TestClient(app(Source(payload), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "iiko_cash_shifts_invalid_response"
        assert "private-error" not in r.text and not list(tmp_path.iterdir())


def test_empty_export_and_append_only_snapshots(tmp_path):
    source = Source(b"[]")
    with TestClient(app(source, tmp_path)) as c:
        first = c.get(BASE, params=PARAMS).json()
        assert first["items"] == [] and first["total"] == 0 and "revision" not in first
        source.body = body()
        second = c.get(BASE, params=PARAMS).json()
        assert second["total"] == 1 and first["snapshot_id"] != second["snapshot_id"]
        source.body = b"broken"
        assert c.get(BASE, params=PARAMS).status_code == 502
        assert len(list(tmp_path.glob("*.json"))) == 4


def test_size_and_disk_failure_clean_unfinished_files(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_cash_shifts_max_response_bytes=10))) as c:
        assert c.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_cash_shifts_too_large"
        assert not list(tmp_path.iterdir())

    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 500 and "private-error" not in r.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_only_401_is_retried_once(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=PARAMS).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/list") for r in source.requests) == len(statuses)


def test_timeout_allows_logout(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/list"):
            await asyncio.sleep(1)
        return httpx.Response(200, text=TOKEN)

    with TestClient(app(handler, tmp_path, config(iiko_cash_shifts_timeout_seconds=0.01))) as c:
        assert c.get(BASE, params=PARAMS).status_code == 504
        assert not list(tmp_path.iterdir())
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_cash_shifts_employees_and_logout_share_one_request_lock(tmp_path):
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
                auth.download_cash_shifts(
                    tmp_path / "shifts.json",
                    open_date_from=date(2026, 9, 9),
                    open_date_to=date(2026, 9, 9),
                    department_ids=[],
                    group_ids=[],
                    status="ANY",
                    revision_from=-1,
                ),
                auth.download_employees(tmp_path / "employees.xml"),
                auth.logout(),
            )
            assert maximum == 1 and paths == ["auth", "list", "employees", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())


def test_openapi_only_exposes_list_reading_and_public_field_names(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        schema = c.get("/openapi.json").json()
        assert {p for p in schema["paths"] if p.startswith(BASE)} == {BASE}
        assert set(schema["paths"][BASE]) == {"get"}
        fields = schema["components"]["schemas"]["CashShift"]["properties"]
        assert "sales_credit" in fields and "salesCredit" not in fields
        assert fields["pay_income"]["examples"] == ["-123.45"]
        assert not any("egais" in p.lower() for p in schema["paths"]) and not source.requests
