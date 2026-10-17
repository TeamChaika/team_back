"""Broker authorization is selected from peer identity, never an HTTP field."""

import hashlib
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app import snapshot_archive_broker as broker

COMPANY_A = "33e403a8-0e0c-498d-aa0d-578146c8afee"
COMPANY_B = "12345678-1234-4234-8234-123456789012"
SCOPE_A = "c_33e403a80e0c498daa0d578146c8afee_analytics"
SNAPSHOT = "12345678-1234-4234-8234-123456789013"


class Storage:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def put(self, key, data):
        if self.fail:
            raise RuntimeError("SECRET PROVIDER RESPONSE")
        self.objects[key] = data

    def get(self, key):
        return self.objects[key]


@pytest.fixture
def connection(monkeypatch):
    import tempfile

    # macOS has a short UDS path limit; host identity is separately unit-tested.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="archive-") as directory:
        path = str(Path(directory) / "broker.sock")
        bound = socket.socket(socket.AF_UNIX)
        bound.bind(path)
        registry = SimpleNamespace(scope=lambda uid: SCOPE_A)
        storage = Storage()
        monkeypatch.setattr(broker, "peer_uid", lambda _: 123)
        server = broker.ArchiveServer(bound, registry, storage)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=path), base_url="http://archive"
            ) as client:
                yield client, registry, storage
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_roundtrip_uses_peer_scope_ignoring_spoofed_headers(connection):
    client, _, storage = connection
    body = b"compressed-envelope"
    digest = hashlib.sha256(body).hexdigest()
    url = f"/v1/snapshots/{SNAPSHOT}/{digest}"
    response = client.put(url, content=body, headers={"X-Tenant": COMPANY_B, "X-UID": "0"})
    assert response.status_code == 200
    key = f"restcontrol/snapshots/v1/{SCOPE_A}/{SNAPSHOT}/{digest}.json.gz"
    assert set(storage.objects) == {key}
    assert response.json()["object_key"] == key
    assert client.get(url).content == body


def test_unknown_uid_denied_before_upload(connection):
    client, registry, storage = connection

    def denied(_):
        raise PermissionError

    registry.scope = denied
    response = client.put(f"/v1/snapshots/{SNAPSHOT}/" + "a" * 64, content=b"not uploaded")
    assert response.status_code == 403
    assert not storage.objects


@pytest.mark.parametrize(
    "path",
    [
        "/v1/snapshots/../../other",
        f"/v1/snapshots/{SNAPSHOT}/" + "a" * 64 + "?scope=chaika",
        f"/v1/snapshots/{SNAPSHOT}/" + "a" * 63,
    ],
)
def test_invalid_routes_rejected(connection, path):
    client, _, storage = connection
    assert client.put(path, content=b"x").status_code == 400
    assert not storage.objects


def test_wrong_digest_and_oversize_rejected(connection, monkeypatch):
    import app.snapshot_archive as archive

    client, _, storage = connection
    url = f"/v1/snapshots/{SNAPSHOT}/" + "a" * 64
    assert client.put(url, content=b"x").status_code == 400
    monkeypatch.setattr(archive, "MAX_OBJECT_BYTES", 1)
    assert client.put(url, content=b"xx").status_code == 413
    assert not storage.objects


def test_s3_error_is_sanitized(connection):
    client, _, storage = connection
    storage.fail = True
    body = b"x"
    url = f"/v1/snapshots/{SNAPSHOT}/{hashlib.sha256(body).hexdigest()}"
    response = client.put(url, content=body)
    assert response.status_code == 503
    assert b"SECRET" not in response.content


def test_grant_derives_scope_and_rechecks_os_account(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "trusted_directory", lambda _: None)
    monkeypatch.setattr(
        broker.os,
        "fstat",
        lambda _: SimpleNamespace(
            st_mode=0o100644,
            st_uid=0,
            st_size=100,
        ),
    )
    monkeypatch.setattr(
        broker.pwd,
        "getpwuid",
        lambda _: SimpleNamespace(
            pw_name=broker.account_name(COMPANY_A),
        ),
    )
    monkeypatch.setattr(broker.pwd, "getpwall", lambda: [SimpleNamespace(pw_uid=123)])
    grant = tmp_path / "123.json"
    grant.write_text(json.dumps({"company_id": COMPANY_A, "uid": 123}))
    registry = broker.GrantRegistry(str(tmp_path))
    assert registry.scope(123) == SCOPE_A
    grant.write_text(json.dumps({"company_id": COMPANY_B, "uid": 123}))
    with pytest.raises(PermissionError):
        registry.scope(123)
    with pytest.raises(PermissionError):
        registry.scope(0)


def test_scope_is_derived_not_accepted_from_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "trusted_directory", lambda _: None)
    monkeypatch.setattr(
        broker.os,
        "fstat",
        lambda _: SimpleNamespace(
            st_mode=0o100644,
            st_uid=0,
            st_size=100,
        ),
    )
    (tmp_path / "123.json").write_text(
        json.dumps(
            {
                "company_id": COMPANY_A,
                "uid": 123,
                "scope": "chaika",
            }
        )
    )
    with pytest.raises(PermissionError):
        broker.GrantRegistry(str(tmp_path)).scope(123)


def test_unsafe_directory_refused(tmp_path):
    with pytest.raises(ValueError):
        broker.trusted_directory(tmp_path)


def test_no_nonlinux_peer_auth_fallback(monkeypatch):
    monkeypatch.setattr(broker.sys, "platform", "darwin")
    with pytest.raises(PermissionError):
        broker.peer_uid(None)


def test_grant_company_ids_are_canonical():
    assert broker.account_name(COMPANY_A) == broker.account_name(UUID(COMPANY_A))


def test_absolute_deadline_closes_incomplete_headers(connection, monkeypatch):
    client, _, _ = connection
    monkeypatch.setattr(broker, "REQUEST_DEADLINE_SECONDS", 0.1)
    # The transport exposes its configured UDS path through the connection pool.
    path = client._transport._pool._uds
    with socket.socket(socket.AF_UNIX) as slow:
        slow.settimeout(2)
        slow.connect(path)
        slow.sendall(b"PUT /v1/snapshots/")
        assert slow.recv(1) == b""
    body = b"healthy"
    digest = hashlib.sha256(body).hexdigest()
    assert client.put(f"/v1/snapshots/{SNAPSHOT}/{digest}", content=body).status_code == 200


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="Linux SO_PEERCRED boundary")
def test_real_linux_peer_credentials():
    import os

    left, right = socket.socketpair(socket.AF_UNIX)
    try:
        assert broker.peer_uid(left) == os.getuid()
        assert broker.peer_uid(right) == os.getuid()
    finally:
        left.close()
        right.close()


def test_archive_migration_refresh_preserves_completed_history_contract():
    from app.saas_admin.provisioning import additive_migration_refresh
    from app.tenancy.migrations import load_migrations

    entries = [(item.name, item.area, item.checksum) for item in load_migrations()]
    assert entries[-1][0] == "20261017120000_tenant_snapshot_archive.sql"
    previous = hashlib.sha256(json.dumps(entries[:-1]).encode()).hexdigest()
    checks = {"migrations": {"ok": True, "evidence": {"manifest_fingerprint": previous}}}
    assert additive_migration_refresh(checks)
    checks["migrations"]["evidence"]["manifest_fingerprint"] = "foreign-prefix"
    assert not additive_migration_refresh(checks)
