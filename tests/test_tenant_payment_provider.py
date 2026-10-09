"""Synthetic provider fixtures only; no keys or payment calls to QR Manager."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.tenant_payments.provider import (
    SANDBOX_URL,
    CreationRejected,
    CreationUnknown,
    QRManagerProvider,
    VerificationUnavailable,
)

OPERATION = UUID("622df995-f45a-473c-b869-d1ab505fdd7a")


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def create(provider):
    return asyncio.run(
        provider.create(
            api_key=SecretStr("synthetic-private-key"),
            amount_minor=12345,
            request_id=uuid4(),
            notification_url="https://example.com/callback/token",
            redirect_url="https://example.com/payment",
        )
    )


def check(provider):
    return asyncio.run(
        provider.check(api_key=SecretStr("synthetic-private-key"), operation_id=OPERATION)
    )


def test_create_documented_fields_and_no_unsupported_idempotency_header():
    def respond(request):
        assert str(request.url) == SANDBOX_URL + "/operations/qr-code/"
        assert request.headers["X-Api-Key"] == "synthetic-private-key"
        assert "X-Correlation-ID" not in request.headers
        assert "Idempotency-Key" not in request.headers
        assert json.loads(request.content) == {
            "sum": 12345,
            "notification_url": "https://example.com/callback/token",
            "redirect_url": "https://example.com/payment",
        }
        return httpx.Response(
            200,
            json={
                "results": {
                    "operation_id": str(OPERATION),
                    "payment_page_link": "https://example.com/pay",
                    "qr_img": "https://example.com/qr.png",
                    "private": "synthetic-private-key",
                }
            },
        )

    result = create(QRManagerProvider(transport=httpx.MockTransport(respond)))
    assert result.operation_id == OPERATION
    assert result.payment_url == "https://example.com/pay"
    assert result.valid_until is None
    assert "synthetic-private-key" not in repr(result)


@pytest.mark.parametrize(
    "status,error",
    [
        (400, CreationRejected),
        (401, CreationRejected),
        (403, CreationRejected),
        (422, CreationRejected),
        (500, CreationUnknown),
        (302, CreationUnknown),
    ],
)
def test_creation_error_is_sanitized_and_no_redirect_or_retry(status, error):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "http://127.0.0.1/private"},
            text="synthetic-private-key provider internals",
        )

    with pytest.raises(error) as caught:
        create(QRManagerProvider(transport=httpx.MockTransport(respond)))
    assert len(requests) == 1
    assert "synthetic-private-key" not in str(caught.value)
    assert "provider internals" not in str(caught.value)


def test_creation_transport_timeout_is_unknown_not_retried():
    requests = []

    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic-private-key", request=request)

    with pytest.raises(CreationUnknown):
        create(QRManagerProvider(transport=httpx.MockTransport(respond)))
    assert len(requests) == 1


@pytest.mark.parametrize(
    "value", ["javascript:alert(1)", "http://example.com", "https://u:p@example.com"]
)
def test_invalid_success_link_is_unknown(value):
    provider = QRManagerProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": {"operation_id": str(OPERATION), "qr_link": value}}
            )
        )
    )
    with pytest.raises(CreationUnknown):
        create(provider)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_sse_split_frames_close_and_do_not_invent_currency(newline):
    body = newline.join(
        [
            b": keepalive",
            b"event: message",
            b'data: {"results":',
            b'data: {"operation_sum":12345,"operation_status_code":5}}',
            b"",
            b"",
        ]
    )
    stream = Chunks([body[i : i + 3] for i in range(0, len(body), 3)])

    def respond(request):
        assert request.url.path == f"/api/v2/sse-operations/{OPERATION}/qr-status/"
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=stream
        )

    result = check(QRManagerProvider(transport=httpx.MockTransport(respond)))
    assert result.operation_id == OPERATION
    assert result.amount_minor == 12345
    assert result.status == "paid"
    assert result.currency is None and result.merchant is None and result.paid_at is None
    assert stream.closed


@pytest.mark.parametrize(
    "code,expected", [(3, "pending"), (4, "pending"), (5, "paid"), (6, "failed"), (8, "failed")]
)
def test_documented_statuses(code, expected):
    provider = QRManagerProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": {"operation_sum": 12345, "operation_status_code": code}}
            )
        )
    )
    assert check(provider).status == expected


@pytest.mark.parametrize(
    "fields",
    [
        {"operation_sum": 12345, "operation_status_code": 0},
        {"operation_sum": 12345, "operation_status_code": 9},
        {"operation_sum": True, "operation_status_code": 5},
        {"operation_sum": 123.45, "operation_status_code": 5},
        {"operation_sum": 12345, "operation_status_code": "5"},
        {"operation_status_code": 5},
        {"operation_sum": 12345, "operation_status_code": 5, "currency": "rub"},
        {"operation_sum": 12345, "operation_status_code": 5, "operation_id": str(uuid4())},
    ],
)
def test_unverifiable_fields_fail_closed(fields):
    provider = QRManagerProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": fields}))
    )
    with pytest.raises(VerificationUnavailable):
        check(provider)


@pytest.mark.parametrize(
    "body",
    [
        b'data: {"results":{"operation_sum":12345,"operation_status_code":5}}',
        b"data: invalid\n\n",
        b"data: \xff\n\n",
        b":" * 2048,
    ],
)
def test_incomplete_malformed_and_oversized_streams_fail_closed(body):
    stream = Chunks([body])
    provider = QRManagerProvider(
        max_response_bytes=1024,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
    )
    with pytest.raises(VerificationUnavailable):
        check(provider)
    assert stream.closed


def test_explicit_provider_currency_is_read_without_fallback_to_invoice_currency():
    provider = QRManagerProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": {
                        "operation_sum": 12345,
                        "operation_status_code": 5,
                        "currency": "RUB",
                        "operation_id": str(OPERATION),
                    }
                },
            )
        )
    )
    assert check(provider).currency == "RUB"


def test_total_stream_deadline_is_bounded_and_closes():
    class SlowStream(Chunks):
        async def __aiter__(self):
            while True:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.005)

    stream = SlowStream([])
    provider = QRManagerProvider(
        timeout=0.02,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
    )
    with pytest.raises(VerificationUnavailable):
        check(provider)
    assert stream.closed


def terminal_payload(**overrides):
    return {
        "merchant_id": "merchant-example",
        "qrt_name": "Synthetic terminal",
        "subscription_end_date": "2027-01-01",
        "qrt_is_b2c": True,
        **overrides,
    }


def terminal(provider):
    return asyncio.run(provider.check_terminal(api_key=SecretStr("synthetic-private-key")))


@pytest.mark.parametrize("mode", ["sandbox", "live"])
def test_authenticated_terminal_check_pinned_environment(mode):
    from app.tenant_payments.provider import LIVE_URL

    requests = []

    def respond(request):
        requests.append(request)
        origin = LIVE_URL if mode == "live" else SANDBOX_URL
        assert str(request.url) == origin + "/users/check-api-key/"
        assert request.method == "GET"
        assert request.headers["X-Api-Key"] == "synthetic-private-key"
        return httpx.Response(200, json=terminal_payload(requires_receipt=False))

    result = terminal(QRManagerProvider(mode=mode, transport=httpx.MockTransport(respond)))
    assert len(requests) == 1
    assert result.merchant_id == "merchant-example"
    assert str(result.subscription_end_date) == "2027-01-01"
    assert result.mode == mode
    assert result.requires_receipt is False
    assert result.is_nomenclature is None and result.is_cash_link is None
    assert "synthetic-private-key" not in repr(result)


@pytest.mark.parametrize("mode", ["production", "https://example.com", "", None])
def test_unknown_environment_rejected(mode):
    with pytest.raises(ValueError):
        QRManagerProvider(mode=mode)


@pytest.mark.parametrize(
    "fields",
    [
        {"merchant_id": None},
        {"merchant_id": 12},
        {"merchant_id": " "},
        {"qrt_name": "x\nprivate"},
        {"qrt_name": "x" * 257},
        {"qrt_name": "x" * 201},
        {"merchant_id": "x" * 201},
        {"subscription_end_date": "20270101"},
        {"subscription_end_date": "2027-02-30"},
        {"qrt_is_b2c": "true"},
        {"qrt_is_b2c": 1},
        {"requires_receipt": None},
        {"requires_receipt": "false"},
        {"is_nomenclature": 0},
        {"is_cash_link": []},
    ],
)
def test_terminal_fields_strict_and_fail_closed(fields):
    provider = QRManagerProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=terminal_payload(**fields))
        )
    )
    with pytest.raises(VerificationUnavailable):
        terminal(provider)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_terminal_failure_sanitized_no_redirect_no_retry(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "http://localhost/private"}, text="synthetic-private-key"
        )

    with pytest.raises(VerificationUnavailable) as caught:
        terminal(QRManagerProvider(transport=httpx.MockTransport(respond)))
    assert len(requests) == 1
    assert "synthetic-private-key" not in str(caught.value)


def test_terminal_expired_or_b2c_disabled_metadata_is_not_hidden():
    provider = QRManagerProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=terminal_payload(
                    subscription_end_date="2000-01-01",
                    qrt_is_b2c=False,
                    requires_receipt=True,
                    is_nomenclature=True,
                    is_cash_link=True,
                ),
            )
        )
    )
    result = terminal(provider)
    assert str(result.subscription_end_date) == "2000-01-01"
    assert result.qrt_is_b2c is False
    assert result.requires_receipt is True and result.is_nomenclature is True


def test_terminal_read_has_size_and_time_limits_and_closes():
    stream = Chunks([b"x" * 2048])
    provider = QRManagerProvider(
        max_response_bytes=1024,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    )
    with pytest.raises(VerificationUnavailable):
        terminal(provider)
    assert stream.closed

    class SlowStream(Chunks):
        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.005)

    slow = SlowStream([])
    provider = QRManagerProvider(
        timeout=0.02,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=slow
            )
        ),
    )
    with pytest.raises(VerificationUnavailable):
        terminal(provider)
    assert slow.closed


def test_live_creation_and_status_use_same_pinned_origin():
    from app.tenant_payments.provider import LIVE_URL

    def respond(request):
        assert str(request.url).startswith(LIVE_URL + "/")
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "operation_id": str(OPERATION),
                        "qr_link": "https://example.com/pay",
                    }
                },
            )
        return httpx.Response(
            200, json={"results": {"operation_sum": 12345, "operation_status_code": 5}}
        )

    provider = QRManagerProvider(mode="live", transport=httpx.MockTransport(respond))
    assert create(provider).operation_id == OPERATION
    assert check(provider).currency is None


QR_PAYLOAD = (
    "https://qr.nspk.ru/AD10102T192IVSCK87ERR4FDK14GNOR7"
    "?type=02&bank=100000000241&sum=12345&cur=RUB&crc=09E8"
)


def created_with_qr_payload(payload, **extras):
    provider = QRManagerProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": {
                        "operation_id": str(OPERATION),
                        "payment_page_link": "https://example.com/pay",
                        "qr_link": payload,
                        **extras,
                    }
                },
            )
        )
    )
    return create(provider)


def test_currency_evidence_comes_only_from_actual_provider_qr_payload():
    result = created_with_qr_payload(QR_PAYLOAD)
    assert result.payment_url == "https://example.com/pay"
    assert result.provider_currency == "RUB"
    assert result.provider_amount_minor == 12345
    assert result.currency_evidence == "sbp_qr_payload"
    assert result.qr_payload == QR_PAYLOAD


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "https://qr.nspk.ru/AD10002ML1STTEO98KC8LRS9BO5FU9P5",
        QR_PAYLOAD.replace("qr.nspk.ru", "attacker.example"),
        QR_PAYLOAD.replace("qr.nspk.ru", "qr.nspk.ru.attacker.example"),
        QR_PAYLOAD.replace("qr.nspk.ru", "user@qr.nspk.ru"),
        QR_PAYLOAD.replace("qr.nspk.ru", "qr.nspk.ru:443"),
        QR_PAYLOAD.replace("https:", "http:"),
        QR_PAYLOAD.replace("sum=12345", "sum=12000"),
        QR_PAYLOAD.replace("sum=12345", "sum=1.2345"),
        QR_PAYLOAD.replace("sum=12345", "sum=+12345"),
        QR_PAYLOAD.replace("sum=12345", "sum="),
        QR_PAYLOAD.replace("cur=RUB", "cur=USD"),
        QR_PAYLOAD.replace("cur=RUB", "cur=rub"),
        QR_PAYLOAD.replace("&cur=RUB", ""),
        QR_PAYLOAD.replace("type=02", "type=01"),
        QR_PAYLOAD.replace("crc=09E8", "crc=invalid"),
        QR_PAYLOAD.replace("bank=100000000241", "bank=1"),
        QR_PAYLOAD + "&cur=RUB",
        QR_PAYLOAD + "&sum=12345",
        QR_PAYLOAD + "#cur=RUB",
        QR_PAYLOAD + "&unknown=value",
    ],
)
def test_missing_or_malformed_qr_evidence_never_invents_currency(payload):
    result = created_with_qr_payload(payload, currency="RUB")
    assert result.operation_id == OPERATION
    assert result.provider_currency is None
    assert result.provider_amount_minor is None
    assert result.currency_evidence is None
    assert result.qr_payload is None
