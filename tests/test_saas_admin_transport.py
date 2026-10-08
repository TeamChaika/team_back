"""Real stdlib HTTPResponse fixtures; synthetic loopback only, never iiko auth."""

import http.client
import ssl
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import certifi
import pytest

from app.saas_admin import connection_check as checker


@contextmanager
def local_response(body, declared_length=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Length", str(len(body) if declared_length is None else declared_length)
            )
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def plain_http_transport(monkeypatch):
    # Replace only TLS construction. Parsing/EOF/socket ownership use real stdlib HTTPResponse.
    monkeypatch.setattr(
        checker,
        "PinnedHTTPS",
        lambda host, port, ip, timeout, deadline: http.client.HTTPConnection(
            host, port, timeout=timeout
        ),
    )


def test_complete_short_response_with_closed_socket_is_returned(monkeypatch):
    body = b"synthetic-token"
    with local_response(body) as port:
        reference = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            reference.request("GET", "/", headers={"Connection": "close"})
            sock = reference.sock
            response = reference.getresponse()
            assert response.read1(4096) == body
            assert response.isclosed()
            assert sock.fileno() == -1
            with pytest.raises(OSError):
                sock.settimeout(1)
        finally:
            reference.close()
        plain_http_transport(monkeypatch)
        assert checker.request("127.0.0.1", port, "127.0.0.1", "/", time.monotonic() + 1) == (
            200,
            body,
        )


def test_truncated_content_length_is_not_accepted_as_token(monkeypatch):
    plain_http_transport(monkeypatch)
    with local_response(b"partial-token", declared_length=100) as port:
        with pytest.raises(checker.CheckFailure) as failure:
            checker.request("127.0.0.1", port, "127.0.0.1", "/", time.monotonic() + 1)
        assert failure.value.code == "unexpected_response"


def test_tls_context_uses_certifi_with_verification_enabled(monkeypatch):
    context_factory = ssl.create_default_context
    seen = []

    def context(**kwargs):
        seen.append(kwargs)
        return context_factory(**kwargs)

    monkeypatch.setattr(ssl, "create_default_context", context)
    connection = checker.PinnedHTTPS("unit.iiko.it", 443, "8.8.8.8", 1, time.monotonic() + 1)
    assert seen == [{"cafile": certifi.where()}]
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True
    assert connection._context.cert_store_stats()["x509_ca"] > 0


@pytest.mark.parametrize(
    "error,code",
    [
        (ssl.SSLCertVerificationError("synthetic-sensitive-detail"), "tls_failed"),
        (TimeoutError("synthetic-sensitive-detail"), "timeout"),
        (ConnectionRefusedError("synthetic-sensitive-detail"), "unreachable"),
    ],
)
def test_transport_errors_are_distinct_and_sanitized(monkeypatch, caplog, error, code):
    class FailingConnection:
        sock = None

        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            raise error

        def close(self):
            pass

    monkeypatch.setattr(checker, "PinnedHTTPS", FailingConnection)
    with pytest.raises(checker.CheckFailure) as failure:
        checker.request("unit.iiko.it", 443, "8.8.8.8", "/", time.monotonic() + 1)
    assert failure.value.code == code
    assert "synthetic-sensitive-detail" not in str(checker.result(code))
    assert "synthetic-sensitive-detail" not in caplog.text
