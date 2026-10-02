"""Synthetic iiko and Telegram responses; no real writes or credentials."""

import logging
from datetime import UTC, datetime
from xml.etree.ElementTree import fromstring

import httpx
import pytest

from app.core.logging import configure_http_logging
from app.documents.config import DocumentSettings
from app.documents.telegram import Telegram
from app.documents.transport import DocumentTransport

TOKEN = "00000000-0000-4000-8000-000000000099"


def settings():
    return DocumentSettings(
        _env_file=None,
        iiko_url="https://iiko.invalid",
        iiko_login="synthetic-user",
        iiko_password_hash="synthetic-secret",
    )


@pytest.mark.parametrize(
    "kind,status,body,expected",
    [
        ("waybill", 200, "<result><valid>true</valid></result>", "sent"),
        ("waybill", 200, "<result><valid>false</valid></result>", "rejected"),
        ("waybill", 503, "upstream unavailable", "unknown"),
        ("writeoff", 200, '{"result":"SUCCESS"}', "sent"),
        ("writeoff", 200, '{"data":{"result":"SUCCESS"}}', "sent"),
        ("writeoff", 500, '{"result":"ERROR"}', "unknown"),
    ],
)
def test_transport_exactly_one_post_and_logout(kind, status, body, expected):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text=TOKEN)
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, text="true")
        assert request.url.params["key"] == TOKEN
        return httpx.Response(status, text=body)

    provider = DocumentTransport(settings(), httpx.MockTransport(respond))
    assert provider.send(kind, b"<document/>" if kind == "waybill" else {}) == expected
    assert [r.method for r in calls] == ["GET", "POST", "GET"]
    assert calls[-1].url.path == "/resto/api/logout"


def test_auth_html_error_never_posts_document():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, text="<html>Service unavailable</html>")

    provider = DocumentTransport(settings(), httpx.MockTransport(respond))
    with pytest.raises(ValueError):
        provider.send("waybill", b"<document/>")
    assert len(calls) == 1


def test_payload_escapes_xml_and_uses_local_business_date():
    doc = {"id": 9, "store_id": "store", "created_at": datetime(2026, 10, 1, 22, tzinfo=UTC)}
    person = {"username": "test", "first_name": "A & B", "last_name": "<C>"}
    xml = DocumentTransport.payload(
        "waybill",
        doc,
        [{"product_id": "product", "amount": 1.5}],
        person,
        person,
        "Store <one>",
        "Store & two",
        None,
    )
    parsed = fromstring(xml)
    assert parsed.findtext("documentNumber") == "DJ000009"
    assert parsed.findtext("dateIncoming") == "2026-10-02T01:00:00+03:00"
    assert "A & B <C>" in parsed.findtext("comment")
    assert parsed.findtext("items/item/amount") == "1.5"
    doc.update(reason_id="Reason", comment="Note")
    payload = DocumentTransport.payload(
        "writeoff", doc, [], person, person, "", "", {"account_id": "account"}
    )
    assert payload["dateIncoming"] == "2026-10-02T01:00:00"


def test_http_logging_masks_iiko_credentials_and_telegram_token(caplog):
    configure_http_logging()
    caplog.set_level(logging.INFO, logger="httpx")
    logging.getLogger("httpx").info(
        "GET https://iiko.invalid/auth?login=secretlogin&pass=secretpass&key=secrettoken"
    )
    bot = Telegram(
        "123456:synthetic-bot-secret",
        httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": []})),
    )
    try:
        bot.call("getUpdates", timeout=0)
    finally:
        bot.close()
    for secret in ["secretlogin", "secretpass", "secrettoken", "synthetic-bot-secret"]:
        assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
