import hashlib
import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from test_iiko_invoices import config

from app.main import create_app

TOKEN = "outgoing-test-token"
PRODUCT = str(UUID(int=100))
STORE = str(UUID(int=200))
RECEIVER = str(UUID(int=300))
LINK = str(UUID(int=400))
BASE = "/api/v1/iiko/outgoing-invoices"
PARAMS = {"date_from": "2026-09-10", "date_to": "2026-09-10"}
ITEM = f"""<item><productId>{PRODUCT}</productId><productArticle>00012</productArticle>
<storeId>{STORE}</storeId><price>89.990000000</price><amount>1.23456789123456789</amount>
<sum>101.123456789123456789</sum><vatPercent>20.000000000</vatPercent>
<vatSum>16.853909465</vatSum><priceWithoutVat>74.991666667</priceWithoutVat></item>"""


def document(doc_id=1, status="PROCESSED", items=ITEM, day="2026-09-10"):
    return f"""<document><id>{UUID(int=doc_id)}</id><documentNumber>00007</documentNumber>
<dateIncoming>{day}T00:00:00</dateIncoming><status>{status}</status>
<defaultStoreId>{STORE}</defaultStoreId><counteragentId>{RECEIVER}</counteragentId>
<linkedIncomingInvoiceId>{LINK}</linkedIncomingInvoiceId><accountToCode>00100</accountToCode>
<comment/><useDefaultDocumentTime>false</useDefaultDocumentTime><items>{items}</items>
<unknown><nested>raw-only</nested></unknown></document>"""


def wrap(documents):
    return f"<outgoingInvoiceDtoes>{documents}</outgoingInvoiceDtoes>".encode()


class Source:
    def __init__(self, body=None):
        self.body = wrap(document()) if body is None else body
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith(("/auth", "/logout")):
            return httpx.Response(200, text=TOKEN)
        assert request.method == "GET"
        assert request.url.path == "/resto/api/documents/export/outgoingInvoice"
        assert request.headers["cookie"] == f"key={TOKEN}"
        assert request.headers["accept"] == "application/xml"
        return httpx.Response(200, content=self.body)


def app(source, folder, settings=None):
    return create_app(
        settings or config(), iiko_transport=httpx.MockTransport(source), outgoing_directory=folder
    )


def test_exact_source_fields_raw_links_statuses_and_repeated_lines(tmp_path):
    source = Source(
        wrap(document(items=ITEM + ITEM) + document(2, "NEW", "") + document(3, "DELETED", ""))
    )
    with TestClient(app(source, tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3 and data["items_count"] == 2
        assert {d["status"] for d in data["documents"]} == {"NEW", "PROCESSED", "DELETED"}
        doc = data["documents"][0]
        assert doc["date_incoming"] == "2026-09-10T00:00:00"
        assert doc["document_number"] == "00007" and doc["account_to_code"] == "00100"
        assert doc["comment"] is None and doc["use_default_document_time"] is False
        assert doc["linked_incoming_invoice_id"] == LINK
        assert doc["linked_outgoing_invoice_id"] is None
        assert doc["counteragent_id"] == RECEIVER
        assert len(doc["items"]) == 2 and doc["items"][0] == doc["items"][1]
        item = doc["items"][0]
        assert item["sum"] == "101.123456789123456789"
        assert item["amount"] == "1.23456789123456789"
        assert item["product_article"] == "00012" and item["product_id"] == PRODUCT
        assert item["vat_sum"] == "16.853909465"
        assert "num" not in item
        assert dict(source.requests[1].url.params) == {"from": "2026-09-10", "to": "2026-09-10"}
        raw = tmp_path / f"{data['snapshot_id']}.xml"
        meta = tmp_path / f"{data['snapshot_id']}.meta.json"
        assert raw.read_bytes() == source.body
        assert hashlib.sha256(source.body).hexdigest() == data["sha256"]
        assert raw.stat().st_mode & 0o777 == meta.stat().st_mode & 0o777 == 0o600
        assert json.loads(meta.read_text())["source_endpoint"] == "documents/export/outgoingInvoice"
        assert "raw-only" not in r.text and TOKEN not in r.text
        assert c.post("/api/v1/iiko/logout").json()["state"] == "logged_out"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"date_from": "2026-09-10"},
        {**PARAMS, "date_to": "2026-09-09"},
        {**PARAMS, "date_to": "2026-09-17"},
        {**PARAMS, "date_from": "2026-02-30"},
        {**PARAMS, "revision_from": -1},
        {**PARAMS, "supplier_id": RECEIVER},
    ],
)
def test_invalid_scope_never_authenticates(tmp_path, params):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=params).status_code == 422
        assert not source.requests


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b"<error>private-error</error>",
        b"<outgoingInvoiceDtoes>",
        b"<!DOCTYPE outgoingInvoiceDtoes><outgoingInvoiceDtoes/>",
        b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><outgoingInvoiceDtoes>&x;</outgoingInvoiceDtoes>',
        wrap(document() + document()),
        wrap(document().replace(f"<id>{UUID(int=1)}</id>", "")),
        wrap(document().replace("<items>", "<items><wrong/>")),
        wrap(
            document().replace(
                "<status>PROCESSED</status>", "<status>PROCESSED</status><status>NEW</status>"
            )
        ),
        wrap(document(items=ITEM.replace("101.123456789123456789", "NaN"))),
        wrap(document(items=ITEM.replace(PRODUCT, "invalid-uuid"))),
        wrap(document(day="2026-09-09")),
    ],
)
def test_bad_xml_and_wrong_dates_keep_previous_snapshot(tmp_path, body):
    source = Source()
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=PARAMS).status_code == 200
        original = set(tmp_path.iterdir())
        source.body = body
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 502 and "private-error" not in r.text
        assert r.json()["error"]["code"] == "iiko_outgoing_invalid_response"
        assert set(tmp_path.iterdir()) == original


def test_empty_export_date_only_and_seven_day_period(tmp_path):
    source = Source(wrap(""))
    with TestClient(app(source, tmp_path)) as c:
        assert c.get(BASE, params=PARAMS).json()["total"] == 0
        source.body = wrap(document().replace("2026-09-10T00:00:00", "2026-09-10"))
        r = c.get(BASE, params={"date_from": "2026-09-04", "date_to": "2026-09-10"})
        assert r.status_code == 200 and r.json()["documents"][0]["date_incoming"] == "2026-09-10"


def test_size_bound_and_disk_failure_do_not_publish(tmp_path, monkeypatch):
    with TestClient(app(Source(), tmp_path, config(iiko_outgoing_max_response_bytes=10))) as c:
        assert c.get(BASE, params=PARAMS).json()["error"]["code"] == "iiko_outgoing_too_large"
        assert not list(tmp_path.iterdir())
    from pathlib import Path

    def fail(*args):
        raise PermissionError("private-path")

    monkeypatch.setattr(Path, "replace", fail)
    with TestClient(app(Source(), tmp_path)) as c:
        r = c.get(BASE, params=PARAMS)
        assert r.status_code == 500 and "private-path" not in r.text
        assert not list(tmp_path.iterdir())
