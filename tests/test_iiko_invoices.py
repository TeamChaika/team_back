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

TOKEN = "invoice-test-token"
PRODUCT = str(UUID(int=100))
SUPPLIER = str(UUID(int=200))
BASE = "/api/v1/iiko/incoming-invoices"
PARAMS = {"date_from": "2026-09-09", "date_to": "2026-09-09"}
ITEM = f"""<item><num>1</num><sum>101.123456789123456789</sum>
<product>{PRODUCT}</product><productArticle>000123</productArticle>
<amount>1.23456789123456789</amount><actualAmount>1.230000000</actualAmount>
<price>89.990000000</price><vatPercent>20.000000000</vatPercent>
<vatSum>16.853909465</vatSum><priceWithoutVat>74.991666667</priceWithoutVat>
<isAdditionalExpense>false</isAdditionalExpense></item>"""


def document(doc_id=1, status="PROCESSED", items=ITEM):
    return f"""<document><id>{UUID(int=doc_id)}</id><documentNumber>00007</documentNumber>
<dateIncoming>2026-09-09T22:35:47</dateIncoming><incomingDate>2026-09-08</incomingDate>
<supplier>{SUPPLIER}</supplier><invoice/><status>{status}</status>
<items>{items}</items><futureField><nested>kept in RAW</nested></futureField></document>"""


def wrap(documents):
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<incomingInvoiceDtoes>{documents}</incomingInvoiceDtoes>"
    ).encode()


def config(**overrides):
    return Settings(
        _env_file=None,
        iiko_base_url="https://iiko.example/resto/api",
        iiko_login="test-api",
        iiko_password="test-secret",
        **overrides,
    )


class Source:
    def __init__(self, body=None, statuses=None):
        self.body = body if body is not None else wrap(document())
        self.statuses = iter(statuses or [200] * 10)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/documents/export/incomingInvoice"
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/xml"
        return httpx.Response(next(self.statuses), content=self.body)


def app(source, directory, settings=None):
    return create_app(
        settings or config(),
        iiko_transport=httpx.MockTransport(source),
        invoices_directory=directory,
    )


def test_xml_preserves_precision_dates_statuses_and_raw(tmp_path):
    source = Source(wrap(document() + document(2, "NEW") + document(3, "DELETED", "")))
    with TestClient(app(source, tmp_path)) as client:
        assert not source.requests
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 3 and result["items_count"] == 2
        assert {doc["status"] for doc in result["documents"]} == {"NEW", "PROCESSED", "DELETED"}
        doc = result["documents"][0]
        assert doc["document_number"] == "00007"
        assert doc["date_incoming"] == "2026-09-09T22:35:47"
        assert doc["incoming_date"] == "2026-09-08"
        assert doc["invoice"] is None and doc["default_store_id"] is None
        item = doc["items"][0]
        assert item["sum"] == "101.123456789123456789"
        assert item["amount"] == "1.23456789123456789"
        assert item["product_article"] == "000123"
        assert item["product_id"] == PRODUCT
        assert item["is_additional_expense"] is False
        assert item["vat_sum"] == "16.853909465"
        assert result["request"] == {**PARAMS, "supplier_id": [], "revision_from": -1}
        assert dict(source.requests[1].url.params) == {
            "from": "2026-09-09",
            "to": "2026-09-09",
            "revisionFrom": "-1",
        }
        raw = tmp_path / f"{result['snapshot_id']}.xml"
        meta = tmp_path / f"{result['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert result["sha256"] == hashlib.sha256(source.body).hexdigest()
        assert result["source_bytes"] == len(source.body)
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        assert json.loads(meta.read_text())["request"] == result["request"]
        assert TOKEN not in response.text and TOKEN not in meta.read_text()
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_repeated_suppliers_revision_and_seven_day_boundary(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(
            BASE,
            params={
                "date_from": "2026-09-03",
                "date_to": "2026-09-09",
                "supplier_id": [SUPPLIER, str(UUID(int=201))],
                "revision_from": 123,
            },
        )
        assert response.status_code == 200
        params = source.requests[1].url.params
        assert params.get_list("supplierId") == [SUPPLIER, str(UUID(int=201))]
        assert params["revisionFrom"] == "123"
        assert params["from"] == "2026-09-03" and params["to"] == "2026-09-09"
        schema = client.get("/openapi.json").json()
        properties = schema["components"]["schemas"]["IncomingInvoice"]["properties"]
        assert "document_number" in properties and "documentNumber" not in properties


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"date_from": "2026-09-09"},
        {**PARAMS, "date_to": "2026-09-08"},
        {**PARAMS, "date_to": "2026-09-16"},
        {**PARAMS, "date_from": "2026-02-30"},
        {**PARAMS, "supplier_id": "bad"},
        {**PARAMS, "supplier_id": [SUPPLIER] * 21},
        {**PARAMS, "revision_from": -2},
        {**PARAMS, "revision_from": "1.1"},
        {**PARAMS, "supplier": SUPPLIER},
    ],
)
def test_invalid_params_do_not_request_token(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=params).status_code == 422
        assert not source.requests


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b"<incomingInvoiceDtoes>",
        b"<error>private-error</error>",
        b"<html>private-error</html>",
        b"<incomingInvoiceDtoes><unexpected/></incomingInvoiceDtoes>",
        b"<!DOCTYPE incomingInvoiceDtoes><incomingInvoiceDtoes/>",
        b'<!DOCTYPE x [<!ENTITY x "private-error">]>'
        b"<incomingInvoiceDtoes>&x;</incomingInvoiceDtoes>",
        b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><incomingInvoiceDtoes>&x;</incomingInvoiceDtoes>',
        wrap(document().replace(f"<id>{UUID(int=1)}</id>", "")),
        wrap(document(items=ITEM.replace("<num>1</num>", ""))),
        wrap(document(items=ITEM.replace("101.123456789123456789", "NaN"))),
        wrap(document(items=ITEM.replace(PRODUCT, "invalid-uuid"))),
        wrap(document() + document()),
        wrap(
            document().replace(
                "<status>PROCESSED</status>", "<status>PROCESSED</status><status>NEW</status>"
            )
        ),
        wrap(document().replace("<items>", "<items><wrong/>")),
        wrap(document().replace("<supplier>", "<supplier><nested/>")),
    ],
)
def test_reject_bad_xml_without_leaking_or_publishing(tmp_path, body):
    source = Source(body)
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "iiko_invoices_invalid_response"
        assert "private-error" not in response.text
        assert not list(tmp_path.iterdir())


def test_repeated_source_line_numbers_are_preserved(tmp_path):
    body = wrap(
        document(items=ITEM + ITEM.replace("<sum>101.123456789123456789</sum>", "<sum>25.5</sum>"))
    )
    source = Source(body)
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 200
        result = response.json()
        assert result["items_count"] == 2
        assert [i["num"] for i in result["documents"][0]["items"]] == [1, 1]
        assert [i["sum"] for i in result["documents"][0]["items"]] == [
            "101.123456789123456789",
            "25.5",
        ]
        assert (tmp_path / f"{result['snapshot_id']}.xml").read_bytes() == body


def test_empty_result_and_article_only_line_are_supported(tmp_path):
    source = Source(wrap(""))
    with TestClient(app(source, tmp_path)) as client:
        result = client.get(BASE, params=PARAMS).json()
        assert result["total"] == result["items_count"] == 0 and result["documents"] == []
        source.body = wrap(document(items=ITEM.replace(f"<product>{PRODUCT}</product>", "")))
        result = client.get(BASE, params=PARAMS).json()
        assert result["documents"][0]["items"][0]["product_id"] is None
        assert result["documents"][0]["items"][0]["product_article"] == "000123"


def test_failed_next_download_preserves_previous_snapshot(tmp_path):
    source = Source()
    with TestClient(app(source, tmp_path)) as client:
        first = client.get(BASE, params=PARAMS).json()
        source.body = b"broken"
        assert client.get(BASE, params=PARAMS).status_code == 502
        assert {p.name for p in tmp_path.iterdir()} == {
            f"{first['snapshot_id']}.xml",
            f"{first['snapshot_id']}.meta.json",
        }


def test_midnight_is_not_silently_converted_to_date(tmp_path):
    source = Source(
        wrap(document().replace("2026-09-08</incomingDate>", "2026-09-08T00:00:00</incomingDate>"))
    )
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.json()["documents"][0]["incoming_date"] == "2026-09-08T00:00:00"


def test_observed_literal_null_due_date_keeps_text_fields_unchanged(tmp_path):
    source = Source(
        wrap(
            document().replace(
                "<invoice/>", "<invoice/><dueDate>null</dueDate><comment>null</comment>"
            )
        )
    )
    with TestClient(app(source, tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 200
        doc = response.json()["documents"][0]
        assert doc["due_date"] is None and doc["comment"] == "null"


def test_read_timeout_removes_partial_xml_and_allows_logout(tmp_path):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"<incomingInvoiceDtoes>"
            await asyncio.sleep(1)

    def handler(request):
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(200, stream=SlowStream())

    with TestClient(app(handler, tmp_path, config(iiko_invoices_timeout_seconds=0.01))) as client:
        assert client.get(BASE, params=PARAMS).status_code == 504
        assert not list(tmp_path.iterdir())
        assert client.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


def test_size_limit_and_disk_failure(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_invoices_max_response_bytes=20))) as client:
        assert client.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_invoices_too_large"
        assert not list(tmp_path.iterdir())

    def broken_replace(path, target):
        raise PermissionError("private-error")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with TestClient(app(Source(), tmp_path)) as client:
        response = client.get(BASE, params=PARAMS)
        assert response.status_code == 500 and "private-error" not in response.text
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("statuses", [[401, 200], [401, 401], [403], [500]])
def test_bounded_retry_on_401_only(tmp_path, statuses):
    source = Source(statuses=statuses)
    with TestClient(app(source, tmp_path)) as client:
        assert client.get(BASE, params=PARAMS).status_code == (200 if statuses[-1] == 200 else 502)
        assert sum(r.url.path.endswith("/incomingInvoice") for r in source.requests) == len(
            statuses
        )


def test_invoices_groups_and_logout_are_sequential(tmp_path):
    async def scenario():
        paths = []
        active = maximum = 0

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                await asyncio.sleep(0.01)
                yield b"<incomingInvoiceDtoes/>"
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
                auth.download_incoming_invoices(
                    tmp_path / "invoices.xml",
                    date_from=date(2026, 9, 9),
                    date_to=date(2026, 9, 9),
                    supplier_ids=[],
                    revision_from=-1,
                ),
                auth.download_groups(tmp_path / "groups.json"),
                auth.logout(),
            )
            assert maximum == 1
            assert paths == ["auth", "incomingInvoice", "list", "logout"]
        finally:
            await auth.aclose()

    asyncio.run(scenario())
