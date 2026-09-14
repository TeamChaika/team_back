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

TOKEN = "assembly-test-token"
PRODUCT = str(UUID(int=2))
DEPARTMENT = str(UUID(int=20))
BASE = "/api/v1/iiko/assembly-charts/assembled"
PARAMS = {"product_id": PRODUCT, "date": "2026-09-10"}


def config(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="test-api",
        iiko_password="test-secret",
        **overrides,
    )


def response_body() -> dict:
    return {
        "knownRevision": -1,
        "assemblyCharts": [
            {
                "id": str(UUID(int=1)),
                "assembledProductId": PRODUCT,
                "dateFrom": "2026-01-01",
                "dateTo": None,
                "assembledAmount": 2,
                "effectiveDirectWriteoffStoreSpecification": {"departments": [], "inverse": False},
                "productSizeAssemblyStrategy": "COMMON",
                "productWriteoffStrategy": "ASSEMBLE",
                "items": [
                    {
                        "id": str(UUID(int=3)),
                        "sortWeight": 0,
                        "productId": str(UUID(int=4)),
                        "productSizeSpecification": None,
                        "storeSpecification": {"departments": [DEPARTMENT], "inverse": True},
                        "amountIn": "0.123456789123456789",
                        "amountMiddle": 0.1,
                        "amountOut": 0.09,
                        "amountIn1": 0.5,
                        "packageCount": 0.25,
                        "packageTypeId": str(UUID(int=10)),
                    }
                ],
                "technologyDescription": "Не менять текст <b>из источника</b>",
                "description": None,
                "futureField": {"keep": "in raw"},
            }
        ],
        "preparedCharts": None,
        "deletedAssemblyChartIds": None,
        "deletedPreparedChartIds": None,
    }


class Source:
    def __init__(self):
        self.body = (
            json.dumps(response_body())
            .replace('"0.123456789123456789"', "0.123456789123456789")
            .encode()
        )
        self.requests = []
        self.status = 200

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/v2/assemblyCharts/getAssembled"
        assert request.headers["cookie"] == f"key={TOKEN}"
        return httpx.Response(self.status, content=self.body)


def app(source, directory: Path, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        assembly_directory=directory,
    )


@pytest.mark.parametrize("department", [None, DEPARTMENT])
def test_get_assembled_preserves_original_units_scopes_and_request(
    tmp_path: Path, department
) -> None:
    source = Source()
    params = {**PARAMS, **({"department_id": department} if department else {})}
    with TestClient(app(source, tmp_path)) as client:
        assert not source.requests
        response = client.get(BASE, params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["product_id"] == PRODUCT
        assert result["business_date"] == "2026-09-10"
        assert result["department_id"] == department
        assert result["known_revision"] == -1
        chart = result["chart"]
        assert chart["date_from"] == "2026-01-01" and chart["date_to"] is None
        assert chart["assembled_amount"] == "2"
        item = chart["items"][0]
        assert item["amount_in"] == "0.123456789123456789"
        assert item["amount_middle"] == "0.1" and item["amount_out"] == "0.09"
        assert item["store_specification"] == {"departments": [DEPARTMENT], "inverse": True}
        assert item["package_count"] == "0.25"
        assert (
            chart["technology_description"]
            == response_body()["assemblyCharts"][0]["technologyDescription"]
        )
        expected = {"productId": PRODUCT, "date": "2026-09-10"}
        if department:
            expected["departmentId"] = department
        assert dict(source.requests[1].url.params) == expected
        assert TOKEN not in response.text
        raw = tmp_path / f"{result['snapshot_id']}.json"
        meta = tmp_path / f"{result['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert hashlib.sha256(raw.read_bytes()).hexdigest() == result["sha256"]
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        assert json.loads(meta.read_text())["business_date"] == PARAMS["date"]
        assert client.get(BASE, params=params).json()["snapshot_id"] != result["snapshot_id"]
        assert len(source.requests) == 3  # auth и два отдельных чтения карты
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_no_chart_is_explicit_null(tmp_path: Path) -> None:
    source = Source()
    body = response_body()
    body["assemblyCharts"] = []
    source.body = json.dumps(body).encode()
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 200 and response.json()["chart"] is None


@pytest.mark.parametrize(
    "specification",
    [None, {"departments": [], "inverse": False}, {"departments": [], "inverse": True}],
)
def test_null_and_empty_including_excluding_specs_remain_distinct(
    tmp_path: Path, specification
) -> None:
    source = Source()
    body = response_body()
    body["assemblyCharts"][0]["items"][0]["storeSpecification"] = specification
    source.body = json.dumps(body).encode()
    with TestClient(app(source, tmp_path)) as client:
        item = client.get(BASE, params=PARAMS).json()["chart"]["items"][0]
        assert item["store_specification"] == specification


@pytest.mark.parametrize(
    "invalid",
    [
        "wrong_product",
        "date_to",
        "future",
        "duplicate_row",
        "multiple_charts",
        "revision",
        "missing_items",
        "invalid_scope",
    ],
)
def test_unexpected_contract_is_rejected_safely(tmp_path: Path, invalid: str) -> None:
    source = Source()
    body = response_body()
    chart = body["assemblyCharts"][0]
    if invalid == "wrong_product":
        chart["assembledProductId"] = str(UUID(int=50))
    elif invalid == "date_to":
        chart["dateTo"] = PARAMS["date"]
    elif invalid == "future":
        chart["dateFrom"] = "2027-01-01"
    elif invalid == "duplicate_row":
        chart["items"].append(chart["items"][0])
    elif invalid == "multiple_charts":
        body["assemblyCharts"].append(chart)
    elif invalid == "revision":
        body["knownRevision"] = 100
    elif invalid == "missing_items":
        del chart["items"]
    else:
        chart["items"][0]["storeSpecification"]["inverse"] = "true"
    source.body = json.dumps(body).encode()
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_assembly_invalid_response"
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "body", [b"[]", b"{", b"null", b'{"knownRevision":-1}', b"private-error-body"]
)
def test_malformed_body_does_not_leak_or_leave_partial_files(tmp_path: Path, body: bytes) -> None:
    source = Source()
    source.body = body
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 502
        assert "private-error-body" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"product_id": "bad", "date": "2026-09-10"},
        {"product_id": PRODUCT, "date": "2026-02-30"},
        {**PARAMS, "department_id": "bad"},
    ],
)
def test_bad_parameters_never_reach_iiko(tmp_path: Path, params: dict) -> None:
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 422
        assert not source.requests


def test_equal_sort_weights_and_repeated_ingredients_are_allowed(tmp_path: Path) -> None:
    source = Source()
    body = response_body()
    items = body["assemblyCharts"][0]["items"]
    items.append({**items[0], "id": str(UUID(int=5))})
    source.body = json.dumps(body).encode()
    with TestClient(app(source, tmp_path)) as client:
        assert len(client.get(BASE, params=PARAMS).json()["chart"]["items"]) == 2


def test_response_limit_and_disk_error_are_safe(tmp_path: Path, monkeypatch) -> None:
    with TestClient(app(Source(), tmp_path, config(iiko_assembly_max_response_bytes=20))) as client:
        assert client.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_assembly_too_large"

    def broken_replace(path, target):
        raise PermissionError("private-error-body")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 500 and "private-error-body" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403]])
def test_assembled_uses_shared_bounded_reauthentication(tmp_path: Path, statuses) -> None:
    calls = []
    remaining = iter(statuses)

    def handler(request):
        calls.append(request.url.path)
        if calls[-1].endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(next(remaining), json=response_body())

    with TestClient(app(handler, tmp_path)) as client:
        assert client.get(BASE, params=PARAMS).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(path.endswith("/getAssembled") for path in calls) == len(statuses)


def test_assembled_groups_and_logout_are_sequential(tmp_path: Path) -> None:
    async def scenario():
        paths = []
        active = maximum = 0

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"{}"
                active -= 1

        def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            paths.append(request.url.path.rsplit("/", 1)[-1])
            if paths[-1] in {"auth", "logout"}:
                active -= 1
                return httpx.Response(200, text=TOKEN)
            return httpx.Response(200, stream=Stream())

        settings = config()
        auth = IikoAuthService(settings, IikoClient(settings, httpx.MockTransport(handler)))
        try:
            await asyncio.gather(
                auth.download_assembled(
                    tmp_path / "chart.json",
                    product_id=UUID(PRODUCT),
                    business_date=date(2026, 9, 10),
                ),
                auth.download_groups(tmp_path / "groups.json"),
                auth.logout(),
            )
            assert maximum == 1
            assert paths == ["auth", "getAssembled", "list", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())
