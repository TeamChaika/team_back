"""Explicit authentication-only check with pinned public DNS and bounded HTTPS I/O."""

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

import certifi

from .models import metadata_url

DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saas-dns")
DNS_SLOTS = threading.BoundedSemaphore(2)
CHECK_SLOTS = threading.BoundedSemaphore(2)

MESSAGES = {
    "ok": "Вход выполнен, сессия iiko освобождена",
    "unsafe_address": "Проверка разрешена только для публичного HTTPS-сервера",
    "dns_failed": "Не удалось определить адрес сервера",
    "unavailable": "Сервер недоступен или не ответил вовремя",
    "tls_failed": "Не удалось подтвердить защищённое соединение с сервером",
    "timeout": "Сервер не ответил за отведённое время",
    "unreachable": "Не удалось подключиться к серверу",
    "auth_failed": "Сервер не подтвердил логин и пароль",
    "unexpected_response": "Сервер вернул неподдерживаемый ответ",
    "logout_failed": "Вход выполнен, но освобождение сессии не подтверждено",
}


class CheckFailure(Exception):
    def __init__(self, code):
        self.code = code


def result(code):
    return {
        "status": "ok" if code == "ok" else "failed",
        "code": code,
        "message": MESSAGES[code],
        "checked_at": datetime.now(UTC).isoformat(),
    }


def resolve_public(host, port):
    if not DNS_SLOTS.acquire(blocking=False):
        raise CheckFailure("unavailable")

    def lookup():
        try:
            return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        finally:
            DNS_SLOTS.release()

    future = DNS_POOL.submit(lookup)
    try:
        addresses = future.result(timeout=2)
    except (OSError, TimeoutError):
        raise CheckFailure("dns_failed") from None
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips:
        raise CheckFailure("dns_failed")
    # Reject the entire answer set if any address is non-public, not only the selected one.
    if any(
        not (address := ipaddress.ip_address(ip)).is_global
        or address.is_multicast
        or address.is_reserved
        for ip in ips
    ):
        raise CheckFailure("unsafe_address")
    return ips[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, deadline):
        super().__init__(
            host, port, timeout=timeout, context=ssl.create_default_context(cafile=certifi.where())
        )
        self.pinned_ip = ip
        self.deadline = deadline

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), timeout=self.timeout)
        self.sock = sock
        try:
            if time.monotonic() >= self.deadline:
                raise TimeoutError()
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.sock.do_handshake()
        except BaseException:
            sock.close()
            raise


def request(
    host,
    port,
    ip,
    path,
    deadline,
    headers=None,
    *,
    method="GET",
    body=None,
    max_bytes=16384,
    read_timeout=5,
):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CheckFailure("timeout")
    connection = PinnedHTTPS(host, port, ip, min(5, remaining), deadline)
    held_socket = [None]
    response = None
    deadline_hit = threading.Event()

    def abort():
        deadline_hit.set()
        sock = connection.sock or held_socket[0]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    watchdog = threading.Timer(max(0, deadline - time.monotonic()), abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={"Accept": "text/plain", "Connection": "close", **(headers or {})},
        )
        sock = connection.sock
        held_socket[0] = sock
        if sock:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure("timeout")
            sock.settimeout(min(read_timeout, remaining))
        response = connection.getresponse()
        body = bytearray()
        while not response.isclosed():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure("timeout")
            if sock:
                sock.settimeout(min(read_timeout, remaining))
            part = response.read1(4096)
            if not part:
                break
            body.extend(part)
            if len(body) > max_bytes:
                raise CheckFailure("unexpected_response")
        if response.length not in (None, 0):
            raise CheckFailure("unexpected_response")
        return response.status, bytes(body)
    except ssl.SSLError:
        raise CheckFailure("tls_failed") from None
    except TimeoutError:
        raise CheckFailure("timeout") from None
    except OSError:
        code = "timeout" if deadline_hit.is_set() or time.monotonic() >= deadline else "unreachable"
        raise CheckFailure(code) from None
    except http.client.HTTPException:
        raise CheckFailure("unexpected_response") from None
    finally:
        watchdog.cancel()
        if response is not None:
            response.close()
        connection.close()


def _check_connection(url, login, password):
    token = None
    code = "unavailable"
    try:
        normalized = metadata_url(url)
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or parsed.port not in (None, 443):
            raise CheckFailure("unsafe_address")
        host, port = parsed.hostname, parsed.port or 443
        ip = resolve_public(host, port)
        root = parsed.path.rstrip("/")
        if root not in ("", "/resto", "/resto/api"):
            raise CheckFailure("unsafe_address")
        if root.endswith("/resto/api"):
            root = root[:-10]
        elif root.endswith("/resto"):
            root = root[:-6]
        api = root + "/resto/api"
        # iiko expects SHA-1 of the password; hash/token exist only in this call's memory.
        params = urlencode({"login": login, "pass": hashlib.sha1(password.encode()).hexdigest()})
        status, body = request(host, port, ip, api + "/auth?" + params, time.monotonic() + 10)
        if status in (401, 403):
            raise CheckFailure("auth_failed")
        if status != 200:
            raise CheckFailure("unexpected_response")
        text = body.decode("ascii", errors="replace").strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", text):
            raise CheckFailure("unexpected_response")
        token = text
        code = "ok"
    except (ValueError, UnicodeError):
        code = "unsafe_address"
    except CheckFailure as exc:
        code = exc.code
    finally:
        if token is not None:
            try:
                status, released = request(
                    host,
                    port,
                    ip,
                    api + "/logout",
                    time.monotonic() + 5,
                    {"Cookie": "key=" + token},
                )
                release_body = released.decode("ascii", errors="replace").strip()
                if status != 200 or release_body not in (token, "Connection released: " + token):
                    code = "logout_failed"
            except CheckFailure:
                code = "logout_failed"
    return result(code)


def check_connection(url, login, password):
    if not CHECK_SLOTS.acquire(blocking=False):
        return result("unavailable")
    try:
        return _check_connection(url, login, password)
    finally:
        CHECK_SLOTS.release()
