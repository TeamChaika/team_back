"""Allowlisted read-only iiko calls with public-IP pinning and confirmed logout."""

import hashlib
import json
import re
import time
from urllib.parse import urlencode, urlsplit

from .connection_check import CheckFailure, request, resolve_public
from .models import metadata_url
from .repository import Problem

ENDPOINTS = {
    "departments": ("corporation/departments?revisionFrom=-1", 4 * 1024 * 1024),
    "columns": ("v2/reports/olap/columns?reportType=SALES", 2 * 1024 * 1024),
    "sales": ("v2/reports/olap", 16 * 1024 * 1024),
}


def target(url):
    parsed = urlsplit(metadata_url(url))
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise Problem(503, "unsafe_source", "Для аналитики нужен публичный HTTPS iikoChain")
    if parsed.path.rstrip("/") not in ("", "/resto", "/resto/api"):
        raise Problem(503, "unsafe_source", "Неподдерживаемый путь подключения iikoChain")
    return parsed.hostname


def fetch(source, operations):
    """A bounded batch uses one licensed session; errors never include URL/secrets/body."""
    token = None
    host = target(source.url)
    try:
        ip = resolve_public(host, 443)
        query = urlencode(
            {
                "login": source.login,
                "pass": hashlib.sha1(source.password.encode(), usedforsecurity=False).hexdigest(),
            }
        )
        status, body = request(host, 443, ip, "/resto/api/auth?" + query, time.monotonic() + 10)
        if status in (401, 403):
            raise CheckFailure("auth_failed")
        if status != 200:
            raise CheckFailure("unexpected_response")
        candidate = body.decode("ascii", errors="replace").strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", candidate):
            raise CheckFailure("unexpected_response")
        token = candidate
        deadline = time.monotonic() + 90
        result = []
        for operation, payload in operations:
            path, maximum = ENDPOINTS[operation]
            status, body = request(
                host,
                443,
                ip,
                "/resto/api/" + path,
                min(deadline, time.monotonic() + 30),
                {
                    "Cookie": "key=" + token,
                    "Accept": "application/xml"
                    if operation == "departments"
                    else "application/json",
                    "Content-Type": "application/json",
                },
                method="POST" if operation == "sales" else "GET",
                body=json.dumps(payload).encode() if operation == "sales" else None,
                max_bytes=maximum,
                read_timeout=30,
            )
            if status != 200:
                raise CheckFailure("unexpected_response")
            result.append(body)
        return result
    except CheckFailure as error:
        raise Problem(
            503, "iiko_" + error.code, "Не удалось получить данные компании из iiko"
        ) from None
    finally:
        if token is not None:
            try:
                status, body = request(
                    host,
                    443,
                    ip,
                    "/resto/api/logout",
                    time.monotonic() + 5,
                    {"Cookie": "key=" + token},
                )
                if status != 200 or body.decode("ascii", errors="replace").strip() not in (
                    token,
                    "Connection released: " + token,
                ):
                    raise CheckFailure("logout_failed")
            except CheckFailure:
                raise Problem(
                    503, "iiko_logout_failed", "iiko не подтвердил освобождение сессии"
                ) from None
