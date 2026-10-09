import hashlib
import json
import socket
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.saas_admin import connection_check as checker
from app.saas_admin.repository import Repository
from app.saas_admin.server import BASE, create_app

ORIGIN = "http://127.0.0.1:8210"
SECRET = "  private-password-secret  "
LOGIN = "private-iiko-login"
URL = "https://unit.iiko.it/resto/api"


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", repository=Repository(tmp_path / "data"))
    app.state.repository.bootstrap("owner", "owner-long-password", "Owner")
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        response = client.post(
            BASE + "/auth/login", json={"username": "owner", "password": "owner-long-password"}
        )
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


def create(client):
    response = client.post(
        BASE + "/companies",
        json={
            "name": "Test company",
            "subscription": {
                "policy": "plans_v1",
                "plan_id": "full",
                "start_date": "2026-01-01",
                "end_date": "2099-12-31",
            },
            "slug": "test-one",
            "chain_url": URL,
            "connection_credentials": {"chain": {"login": LOGIN, "password": SECRET}},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_encrypted_atomic_write_and_no_secrets_in_readback(client):
    company = create(client)
    assert "connection_credentials" not in company
    path = BASE + "/companies/" + company["id"]
    for url in [path, BASE + "/companies", path + "/events", path + "/connections"]:
        text = client.get(url).text
        assert SECRET.strip() not in text
    repo = client.app.state.repository
    with repo.connect() as db:
        all_rows = "\n".join(str(tuple(row)) for row in db.execute("SELECT * FROM connections"))
        assert SECRET.strip() not in all_rows and LOGIN not in all_rows
        body = db.execute("SELECT body FROM companies").fetchone()[0]
        assert "credentials" not in body and LOGIN not in body
        audit = str([tuple(row) for row in db.execute("SELECT * FROM events")])
        assert SECRET.strip() not in audit and LOGIN not in audit
    reopened = Repository(repo.path.parent)
    assert reopened.check_inputs(company["id"], "chain", 1)[2] == SECRET
    assert (repo.path.parent / "credentials.key").stat().st_mode & 0o777 == 0o600
    broken = client.post(
        BASE + "/companies", json={"name": "No secret", "slug": "no-secret", "chain_url": URL}
    )
    assert broken.status_code == 422
    assert client.get(BASE + "/companies").json()["total"] == 1


def test_credential_retention_changes_and_check_invalidation(client):
    company = create(client)
    path = BASE + "/companies/" + company["id"]
    client.app.state.connection_tester = lambda *args: checker.result("ok")
    checked = client.post(path + "/connections/chain/test", json={"expected_version": 1})
    assert checked.json()["company_version"] == 2
    assert client.get(path).json()["integration_state"] == "ok"
    retained = client.patch(
        path, json={"expected_version": 2, "connection_credentials": {"chain": {"login": LOGIN}}}
    )
    assert retained.status_code == 200
    assert client.get(path + "/connections").json()["items"][0]["check"]["status"] == "ok"
    changed_login = client.patch(
        path,
        json={"expected_version": 3, "connection_credentials": {"chain": {"login": "another"}}},
    )
    assert changed_login.status_code == 422
    changed_url = client.patch(
        path, json={"expected_version": 3, "chain_url": "https://other.iiko.it"}
    )
    assert changed_url.status_code == 422
    assert client.get(path).json()["version"] == 3
    password_change = client.patch(
        path,
        json={
            "expected_version": 3,
            "connection_credentials": {"chain": {"login": LOGIN, "password": "changed-secret"}},
        },
    )
    assert password_change.status_code == 200
    assert password_change.json()["integration_state"] == "not_checked"
    assert client.get(path + "/connections").json()["items"][0]["check"]["checked_at"] is None
    assert client.patch(path, json={"expected_version": 4, "chain_url": None}).status_code == 200
    assert client.get(path + "/connections").json()["items"] == []


def test_draft_saved_secret_exact_match_and_auth(client):
    company = create(client)
    seen = []
    client.app.state.connection_tester = lambda *args: seen.append(args) or checker.result("ok")
    payload = {
        "company_id": company["id"],
        "connection_id": "chain",
        "expected_version": 1,
        "url": URL,
        "login": LOGIN,
    }
    assert (
        client.post(
            BASE + "/connections/test", json={**payload, "url": "https://other.iiko.it"}
        ).status_code
        == 422
    )
    assert (
        client.post(BASE + "/connections/test", json={**payload, "expected_version": 2}).status_code
        == 409
    )
    response = client.post(BASE + "/connections/test", json=payload)
    assert response.json()["status"] == "ok"
    assert seen == [(URL, LOGIN, SECRET)]
    assert (
        client.get(BASE + "/companies/" + company["id"]).json()["integration_state"]
        == "not_checked"
    )
    assert client.post(BASE + "/connections/test", json=payload).status_code == 429
    client.headers.pop("X-CSRF-Token")
    assert client.post(BASE + "/connections/test", json=payload).status_code == 403
    client.cookies.clear()
    assert client.get(BASE + "/companies/" + company["id"] + "/connections").status_code == 401


def test_unknown_key_fails_closed(client):
    create(client)
    repo = client.app.state.repository
    key_path = repo.path.parent / "credentials.key"
    key_path.unlink()
    with pytest.raises(ValueError, match="key missing"):
        Repository(repo.path.parent)
    assert not key_path.exists()


def test_migrate_v1_metadata_only_record(tmp_path):
    repo = Repository(tmp_path / "data")
    repo.bootstrap("owner", "owner-long-password", "Owner")
    company_id = str(uuid4())
    old = {
        "id": company_id,
        "name": "Legacy",
        "slug": "legacy-one",
        "domain": None,
        "status": "draft",
        "version": 1,
        "archived_at": None,
        "created_at": "old",
        "updated_at": "old",
        "primary_admin": None,
        "chain_url": URL,
        "rms": [],
        "modules": {},
        "subscription": {"plan": "", "start_date": None, "end_date": None},
        "notes": "",
    }
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "INSERT INTO companies VALUES (?,?,?,?,?,?,?,?)",
            (company_id, "legacy-one", None, "Legacy", "draft", 1, None, json.dumps(old)),
        )
        db.execute("DROP TABLE connections")
        db.execute("PRAGMA user_version=1")
    reopened = Repository(repo.path.parent)
    assert reopened.connections(company_id)["items"][0]["password_set"] is False
    with reopened.connect() as db:
        actor = dict(db.execute("SELECT id,display_name FROM owners").fetchone())
    values = {
        k: v
        for k, v in old.items()
        if k not in ("id", "version", "archived_at", "created_at", "updated_at")
    }
    values["notes"] = "Unrelated edit"
    assert reopened.save(values, actor, company_id, 1)["version"] == 2


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1", "ff02::1", "100.64.0.1"]
)
def test_ssrf_dns_blocks_all_nonpublic(monkeypatch, ip):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )
    with pytest.raises(checker.CheckFailure) as failure:
        checker.resolve_public("unit.iiko.it", 443)
    assert failure.value.code == "unsafe_address"


def test_auth_logout_confirmed_cookie_and_sanitized_errors(monkeypatch):
    monkeypatch.setattr(checker, "resolve_public", lambda *args: "8.8.8.8")
    calls = []
    token = "unit-token"

    def request(host, port, ip, path, deadline, headers=None):
        calls.append((path, headers))
        return (
            (200, token.encode())
            if "/auth?" in path
            else (200, ("Connection released: " + token).encode())
        )

    monkeypatch.setattr(checker, "request", request)
    result = checker.check_connection(URL, LOGIN, SECRET)
    assert result["status"] == "ok"
    assert parse_qs(urlsplit(calls[0][0]).query)["pass"] == [
        hashlib.sha1(SECRET.encode()).hexdigest()
    ]
    assert calls[1] == ("/resto/api/logout", {"Cookie": "key=" + token})
    assert SECRET.strip() not in json.dumps(result) and token not in json.dumps(result)
    monkeypatch.setattr(checker, "request", lambda *args, **kwargs: (401, SECRET.encode()))
    assert checker.check_connection(URL, LOGIN, SECRET)["code"] == "auth_failed"


def test_logout_failure_not_success_no_auth_retry(monkeypatch):
    monkeypatch.setattr(checker, "resolve_public", lambda *args: "8.8.8.8")
    calls = []

    def request(*args, **kwargs):
        calls.append(args[3])
        if len(calls) == 1:
            return 200, b"unit-token"
        raise checker.CheckFailure("unavailable")

    monkeypatch.setattr(checker, "request", request)
    result = checker.check_connection(URL, LOGIN, SECRET)
    assert result["code"] == "logout_failed" and result["status"] == "failed"
    assert len(calls) == 2


def test_redirect_does_not_trigger_second_request(monkeypatch):
    monkeypatch.setattr(checker, "resolve_public", lambda *args: "8.8.8.8")
    calls = []
    monkeypatch.setattr(
        checker, "request", lambda *args, **kwargs: calls.append(args) or (302, b"")
    )
    assert checker.check_connection(URL, LOGIN, SECRET)["status"] == "failed"
    assert len(calls) == 1


def test_absolute_deadline_interrupts_slow_headers(monkeypatch):
    class FakeSocket:
        closed = False

        def settimeout(self, timeout):
            pass

        def shutdown(self, how):
            self.closed = True

        def close(self):
            self.closed = True

    class SlowHeaders:
        def __init__(self, *args):
            self.sock = FakeSocket()

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            while not self.sock.closed:
                time.sleep(0.001)
            raise OSError("synthetic slow response")

        def close(self):
            self.sock.close()

    monkeypatch.setattr(checker, "PinnedHTTPS", SlowHeaders)
    started = time.monotonic()
    with pytest.raises(checker.CheckFailure):
        checker.request("unit.iiko.it", 443, "8.8.8.8", "/resto/api/auth", started + 0.02)
    assert time.monotonic() - started < 1


def test_saved_check_does_not_commit_stale_result(client):
    company = create(client)
    path = BASE + "/companies/" + company["id"]

    def during_check(*args):
        changed = client.patch(path, json={"expected_version": 1, "notes": "changed during check"})
        assert changed.status_code == 200
        return checker.result("ok")

    client.app.state.connection_tester = during_check
    response = client.post(path + "/connections/chain/test", json={"expected_version": 1})
    assert response.status_code == 409
    assert client.get(path + "/connections").json()["items"][0]["check"]["status"] == "not_checked"
    actions = [item["action"] for item in client.get(path + "/events").json()["items"]]
    assert actions == ["updated", "created"]


def test_mixed_public_private_dns_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(checker.CheckFailure) as failure:
        checker.resolve_public("unit.iiko.it", 443)
    assert failure.value.code == "unsafe_address"


def test_tls_transport_pins_numeric_ip_preserves_original_hostname(monkeypatch):
    calls = []

    class FakeSocket:
        def do_handshake(self):
            calls.append(("handshake",))

        def close(self):
            pass

    sock = FakeSocket()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, **kwargs: calls.append(("connect", address)) or sock,
    )
    connection = checker.PinnedHTTPS("unit.iiko.it", 443, "8.8.8.8", 1, time.monotonic() + 1)

    class Context:
        def wrap_socket(self, raw, **kwargs):
            calls.append(("tls", kwargs))
            return raw

    connection._context = Context()
    connection.connect()
    assert calls == [
        ("connect", ("8.8.8.8", 443)),
        ("tls", {"server_hostname": "unit.iiko.it", "do_handshake_on_connect": False}),
        ("handshake",),
    ]


def test_target_cooldown_canonicalizes_root_variants(client):
    client.app.state.connection_tester = lambda *args: checker.result("ok")
    payload = {"url": "https://unit.iiko.it", "login": LOGIN, "password": SECRET}
    assert client.post(BASE + "/connections/test", json=payload).status_code == 200
    for url in ["https://unit.iiko.it/", "https://unit.iiko.it/resto", URL + "/"]:
        assert (
            client.post(BASE + "/connections/test", json={**payload, "url": url}).status_code == 429
        )


def test_replacing_url_requires_password_and_invalidates(client):
    company = create(client)
    path = BASE + "/companies/" + company["id"]
    client.app.state.connection_tester = lambda *args: checker.result("ok")
    assert (
        client.post(path + "/connections/chain/test", json={"expected_version": 1}).status_code
        == 200
    )
    credentials = {"chain": {"login": LOGIN, "password": SECRET}}
    response = client.patch(
        path,
        json={
            "expected_version": 2,
            "chain_url": "https://different.iiko.it",
            "connection_credentials": credentials,
        },
    )
    assert response.status_code == 200
    connection = client.get(path + "/connections").json()["items"][0]
    assert connection["url"] == "https://different.iiko.it"
    assert connection["check"]["status"] == "not_checked"
    assert connection["check"]["checked_at"] is None


def test_rms_credentials_and_archive_removal(client):
    rms_id = str(uuid4())
    response = client.post(
        BASE + "/companies",
        json={
            "name": "RMS company",
            "subscription": {
                "policy": "plans_v1",
                "plan_id": "full",
                "start_date": "2026-01-01",
                "end_date": "2099-12-31",
            },
            "slug": "rms-company",
            "rms": [{"id": rms_id, "label": "Kitchen", "url": URL, "enabled": True}],
            "connection_credentials": {rms_id: {"login": LOGIN, "password": SECRET}},
        },
    )
    assert response.status_code == 201
    path = BASE + "/companies/" + response.json()["id"]
    assert client.get(path + "/connections").json()["items"][0]["id"] == rms_id
    assert client.delete(path + "?expected_version=1").status_code == 204
    with client.app.state.repository.connect() as db:
        assert db.execute("SELECT count(*) FROM connections").fetchone()[0] == 0


def test_saved_check_response_is_transaction_snapshot(client, monkeypatch):
    from contextlib import contextmanager

    from app.saas_admin.models import CompanyWrite

    company = create(client)
    repo = client.app.state.repository
    connect = repo.connect
    with connect() as db:
        actor = dict(db.execute("SELECT id,display_name FROM owners").fetchone())
    fired = False

    @contextmanager
    def connection_with_competing_writer(write=False):
        nonlocal fired
        with connect(write) as db:
            yield db
        if write and not fired:
            fired = True
            values = {k: v for k, v in company.items() if k in CompanyWrite.model_fields}
            repo.save({**values, "chain_url": None}, actor, company["id"], 2)

    monkeypatch.setattr(repo, "connect", connection_with_competing_writer)
    response = repo.record_check(company["id"], "chain", 1, checker.result("ok"), actor)
    assert response["company_version"] == 2
    assert response["connection"]["check"]["status"] == "ok"
    assert response["connection"]["url"] == URL
    assert repo.get(company["id"])["version"] == 3
    assert repo.connections(company["id"])["items"] == []


def test_disabled_rms_does_not_block_verified_chain_aggregate(client):
    rms_id = str(uuid4())
    company = create(client)
    path = BASE + "/companies/" + company["id"]
    client.app.state.connection_tester = lambda *args: checker.result("ok")
    assert (
        client.post(path + "/connections/chain/test", json={"expected_version": 1}).status_code
        == 200
    )
    response = client.patch(
        path,
        json={
            "expected_version": 2,
            "rms": [
                {
                    "id": rms_id,
                    "label": "Disabled",
                    "url": "https://other.iiko.it",
                    "enabled": False,
                }
            ],
            "connection_credentials": {rms_id: {"login": LOGIN, "password": SECRET}},
        },
    )
    assert response.status_code == 200
    assert response.json()["integration_state"] == "ok"
    assert len(client.get(path + "/connections").json()["items"]) == 2
