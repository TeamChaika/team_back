"""Bounded QR Manager transport. No automatic retry of creation requests."""

from __future__ import annotations

import asyncio
import codecs
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

SANDBOX_URL = "https://app.devwapiserv.qrm.ooo"
# Existing production integration's pinned origin; never caller-supplied.
LIVE_URL = "https://app.wapiserv.qrm.ooo"
MAX_RESPONSE_BYTES = 64 * 1024


class ProviderError(Exception):
    """Safe diagnostic: never includes provider content or credentials."""


class CreationRejected(ProviderError):
    """The provider rejected creation; no operation is known to exist."""


class CreationUnknown(ProviderError):
    """Creation may have succeeded. Do not automatically redispatch."""


class VerificationUnavailable(ProviderError):
    """No trustworthy complete status was obtained."""


@dataclass(frozen=True)
class TerminalContext:
    """Authenticated terminal metadata, not evidence that an operation is paid.

    Missing optional fiscal fields remain unknown. Callers must separately assess
    subscription expiry, B2C eligibility and their supported receipt workflow.
    """

    merchant_id: str
    qrt_name: str
    subscription_end_date: date
    qrt_is_b2c: bool
    mode: Literal["sandbox", "live"]
    requires_receipt: bool | None = None
    is_nomenclature: bool | None = None
    is_cash_link: bool | None = None


@dataclass(frozen=True)
class CreatedOperation:
    operation_id: UUID
    payment_url: str
    qr_image: str | None = None
    valid_until: datetime | None = None
    provider_currency: str | None = None
    provider_amount_minor: int | None = None
    currency_evidence: Literal["sbp_qr_payload"] | None = None
    qr_payload: str | None = None


@dataclass(frozen=True)
class VerifiedOperation:
    operation_id: UUID
    amount_minor: int
    # Current SSE schema has no currency. None MUST block settlement.
    currency: str | None
    status: Literal["pending", "paid", "failed"]
    merchant: str | None = None
    paid_at: datetime | None = None


def _https_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid URL")
    if any(ord(c) < 33 for c in value):
        raise ValueError("Invalid URL")
    return value


def _key(value: SecretStr | str) -> str:
    secret = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not isinstance(secret, str) or not secret or any(ord(c) < 33 for c in secret):
        raise ValueError("Invalid provider credential")
    return secret


def _operation_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("Invalid provider operation identifier")
    return UUID(value)


def _qr_currency_evidence(value: object, amount_minor: int) -> tuple[str, int, str] | None:
    """Read currency only from the provider-returned expanded SBP QR payload.

    Bank specification: https://mkb.ru/file/436aee33-f12d-46bc-9bb6-a310758b0b20
    NSD's 2026-08-18 format notice confirms cur/SUM, but newer unified links
    omit both. Those links MUST remain unverifiable here. The QR identifier is
    an NSPK identifier, not the separate QR Manager operation UUID.
    """
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(_https_url(value))
        if (
            parsed.netloc != "qr.nspk.ru"
            or parsed.fragment
            or re.fullmatch(r"/[A-Z0-9]{32}", parsed.path) is None
        ):
            return None
        params = parse_qs(
            parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10
        )
        if set(params) != {"type", "bank", "sum", "cur", "crc"}:
            return None
        if any(len(values) != 1 for values in params.values()):
            return None
        fields = {key: values[0] for key, values in params.items()}
        if (
            fields["type"] != "02"
            or re.fullmatch(r"[0-9]{12}", fields["bank"]) is None
            or re.fullmatch(r"[0-9]{1,12}", fields["sum"]) is None
            or fields["cur"] != "RUB"
            or re.fullmatch(r"[0-9A-F]{4}", fields["crc"]) is None
            or int(fields["sum"]) != amount_minor
        ):
            return None
        # CRC is format-checked, not treated as a signature. Evidence provenance
        # is the authenticated creation response that also supplied operation_id.
        return fields["cur"], int(fields["sum"]), value
    except ValueError:
        return None


class QRManagerProvider:
    """Fixed environment origins only, with injected transport for local tests."""

    def __init__(
        self,
        *,
        mode: Literal["sandbox", "live"] = "sandbox",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if mode not in {"sandbox", "live"}:
            raise ValueError("Invalid provider mode")
        if not 0 < timeout <= 30 or not 0 < max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("Invalid transport bounds")
        self._transport = transport
        self.mode = mode
        self._base_url = SANDBOX_URL if mode == "sandbox" else LIVE_URL
        self._timeout = timeout
        self._max_bytes = max_response_bytes

    async def check_terminal(self, *, api_key: SecretStr | str) -> TerminalContext:
        """Read-only documented authenticated check; never creates a payment."""
        try:
            headers = {
                "X-Api-Key": _key(api_key),
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "GET", self._base_url + "/users/check-api-key/", headers=headers
                    ) as response:
                        if response.status_code != 200:
                            raise VerificationUnavailable("Provider terminal unavailable")
                        if (
                            response.headers.get("content-type", "").split(";", 1)[0].lower()
                            != "application/json"
                        ):
                            raise ValueError("Invalid terminal response format")
                        data = json.loads(await self._read(response))
                        return self._terminal(data)
        except VerificationUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise VerificationUnavailable("Provider terminal unavailable") from None

    def _terminal(self, data: object) -> TerminalContext:
        if not isinstance(data, dict):
            raise ValueError("Invalid terminal response")
        for name in ("merchant_id", "qrt_name"):
            value = data.get(name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 200
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid terminal identity")
        expiry = data.get("subscription_end_date")
        if not isinstance(expiry, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry) is None:
            raise ValueError("Invalid terminal expiry")
        if type(data.get("qrt_is_b2c")) is not bool:
            raise ValueError("Invalid terminal eligibility")
        fiscal = {}
        for name in ("requires_receipt", "is_nomenclature", "is_cash_link"):
            if name in data and type(data[name]) is not bool:
                raise ValueError("Invalid terminal fiscal configuration")
            fiscal[name] = data.get(name)
        return TerminalContext(
            merchant_id=data["merchant_id"],
            qrt_name=data["qrt_name"],
            subscription_end_date=date.fromisoformat(expiry),
            qrt_is_b2c=data["qrt_is_b2c"],
            mode=self.mode,
            **fiscal,
        )

    async def create(
        self,
        *,
        api_key: SecretStr | str,
        amount_minor: int,
        request_id: UUID,
        notification_url: str,
        redirect_url: str,
        currency: str = "RUB",
    ) -> CreatedOperation:
        if type(amount_minor) is not int or amount_minor < 1 or currency != "RUB":
            raise CreationRejected("Invalid payment amount or currency")
        if not isinstance(request_id, UUID):
            raise CreationRejected("Invalid local request identifier")
        try:
            headers = {
                "X-Api-Key": _key(api_key),
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            body = {
                "sum": amount_minor,
                "notification_url": _https_url(notification_url),
                "redirect_url": _https_url(redirect_url),
            }
        except ValueError:
            raise CreationRejected("Invalid payment configuration") from None
        # request_id is local only: this endpoint documents no idempotency key.
        try:
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST", self._base_url + "/operations/qr-code/", headers=headers, json=body
                    ) as response:
                        if response.status_code in {400, 401, 403, 422}:
                            raise CreationRejected("Provider rejected payment creation")
                        if response.status_code != 200:
                            raise CreationUnknown("Payment creation outcome is unknown")
                        data = json.loads(await self._read(response))
                        result = data["results"]
                        operation = _operation_uuid(result["operation_id"])
                        payment_url = _https_url(
                            result.get("payment_page_link") or result.get("qr_link")
                        )
                        qr_image = _https_url(result["qr_img"]) if result.get("qr_img") else None
                        evidence = _qr_currency_evidence(result.get("qr_link"), amount_minor)
                        return CreatedOperation(
                            operation,
                            payment_url,
                            qr_image,
                            provider_currency=evidence[0] if evidence else None,
                            provider_amount_minor=evidence[1] if evidence else None,
                            currency_evidence="sbp_qr_payload" if evidence else None,
                            qr_payload=evidence[2] if evidence else None,
                        )
        except (CreationRejected, CreationUnknown):
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise CreationUnknown("Payment creation outcome is unknown") from None

    async def check(self, *, api_key: SecretStr | str, operation_id: UUID) -> VerifiedOperation:
        try:
            if not isinstance(operation_id, UUID):
                raise ValueError("Invalid operation")
            headers = {
                "X-Api-Key": _key(api_key),
                "Accept": "text/event-stream, application/json",
                "Accept-Encoding": "identity",
            }
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    path = f"/api/v2/sse-operations/{operation_id}/qr-status/"
                    async with client.stream(
                        "GET", self._base_url + path, headers=headers
                    ) as response:
                        if response.status_code != 200:
                            raise VerificationUnavailable("Provider status unavailable")
                        self._validate_encoding(response)
                        content_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].lower()
                        )
                        if content_type == "application/json":
                            return self._status(
                                json.loads(await self._read(response)), operation_id
                            )
                        if content_type != "text/event-stream":
                            raise VerificationUnavailable("Invalid provider status format")
                        return await self._sse(response, operation_id)
        except VerificationUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise VerificationUnavailable("Provider status unavailable") from None

    async def _read(self, response: httpx.Response) -> bytes:
        self._validate_encoding(response)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self._max_bytes:
                raise ValueError("Response limit exceeded")
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _validate_encoding(response: httpx.Response) -> None:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            # Do not let compressed chunks inflate before the size limit is checked.
            raise ValueError("Unsupported provider content encoding")

    def _status(self, data: object, operation_id: UUID) -> VerifiedOperation:
        if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
            raise ValueError("Invalid status event")
        result = data["results"]
        code, amount = result.get("operation_status_code"), result.get("operation_sum")
        if type(code) is not int or type(amount) is not int or amount < 1:
            raise ValueError("Invalid status fields")
        statuses = {3: "pending", 4: "pending", 5: "paid", 6: "failed", 8: "failed"}
        if code not in statuses:
            # 0 is stream waiting timeout, not evidence of failed payment.
            raise VerificationUnavailable("Provider returned no current operation status")
        currency = result.get("currency")
        if currency is not None and (
            not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None
        ):
            raise ValueError("Invalid provider currency")
        if "operation_id" in result and _operation_uuid(result["operation_id"]) != operation_id:
            raise ValueError("Unexpected provider operation")
        return VerifiedOperation(operation_id, amount, currency, statuses[code])

    async def _sse(self, response: httpx.Response, operation_id: UUID) -> VerifiedOperation:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending, event_data, count = "", [], 0
        async for chunk in response.aiter_bytes():
            count += len(chunk)
            if count > self._max_bytes:
                raise ValueError("Response limit exceeded")
            pending += decoder.decode(chunk)
            while True:
                indices = [i for i in (pending.find("\n"), pending.find("\r")) if i >= 0]
                if not indices:
                    break
                index = min(indices)
                # Hold final CR until next chunk to distinguish CRLF from CR.
                if pending[index] == "\r" and index == len(pending) - 1:
                    break
                width = 2 if pending[index : index + 2] == "\r\n" else 1
                line, pending = pending[:index], pending[index + width :]
                if not line:
                    if event_data:
                        return self._status(json.loads("\n".join(event_data)), operation_id)
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                if field == "data":
                    event_data.append(value[1:] if value.startswith(" ") else value)
        decoder.decode(b"", final=True)
        if pending == "\r" and event_data:
            return self._status(json.loads("\n".join(event_data)), operation_id)
        # Incomplete frames cannot establish status, even when their JSON is valid.
        raise VerificationUnavailable("Provider status stream ended without a complete event")


# Compatibility name for the integrating service.
QRMProvider = QRManagerProvider
