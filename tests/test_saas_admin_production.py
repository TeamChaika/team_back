"""Production origin, cookie, storage and restore acceptance without network calls."""

import sqlite3

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.saas_admin.lifecycle import restore_registry, snapshot_registry, validate_registry
from app.saas_admin.repository import Repository
from app.saas_admin.server import BASE, create_app

ORIGIN = "https://rc.chaika.team"
PASSWORD = "owner-production-test-password"


@pytest.fixture
def registry(tmp_path):
    root = tmp_path / "registry"
    repo = Repository(root)
    repo.bootstrap("owner", PASSWORD, "Owner")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "saas-admin.html").write_text("<html>RestControl</html>")
    return root, dist


def client_for(registry):
    root, dist = registry
    app = create_app(root, dist, ORIGIN, "production", repository=Repository(root))
    client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    return client


def enter(client):
    response = client.post(BASE + "/auth/login", json={"username": "owner", "password": PASSWORD})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def secure_cookie(response):
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie


@pytest.mark.parametrize(
    "origin",
    [
        "http://rc.chaika.team",
        "https://rc.chaika.team/",
        "https://rc.chaika.team:443",
        "https://rc.chaika.team?x",
        "https://user@rc.chaika.team",
        "https://127.0.0.1",
        "https://rc.local",
        "https://rc..team",
        "https://RC.chaika.team",
    ],
)
def test_bad_production_origins_do_not_create_registry(tmp_path, origin):
    target = tmp_path / "missing"
    with pytest.raises(ValueError):
        create_app(target, origin=origin, mode="production")
    assert not target.exists()


def test_required_production_assets_and_storage(registry, tmp_path):
    root, dist = registry
    with pytest.raises(ValueError):
        create_app(tmp_path / "missing", dist, ORIGIN, "production")
    with pytest.raises(ValueError):
        create_app(root, None, ORIGIN, "production")
    (dist / "saas-admin.html").unlink()
    with pytest.raises(ValueError):
        create_app(root, dist, ORIGIN, "production")
    assert not (tmp_path / "missing").exists()


def test_production_boundary_and_owner_cookie(registry):
    client = client_for(registry)
    secure_cookie(enter(client))
    health = client.get(BASE + "/health")
    assert health.json()["mode"] == "production"
    assert health.headers["strict-transport-security"] == "max-age=31536000"
    assert "access-control-allow-origin" not in health.headers
    for headers in (
        {"Host": "evil.example"},
        {"Host": "localhost", "X-Forwarded-Host": "rc.chaika.team"},
    ):
        assert client.get(BASE + "/health", headers=headers).status_code == 403
    assert (
        client.post(BASE + "/auth/logout", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.post(BASE + "/auth/logout", headers={"X-CSRF-Token": "bad"}).status_code == 403
    response = client.post(BASE + "/auth/logout")
    assert response.status_code == 204
    secure_cookie(response)


def prepare_company(client):
    company = client.post(
        BASE + "/companies",
        json={
            "name": "Private company",
            "slug": "tenant-one",
            "chain_url": "https://unit.iiko.it",
            "connection_credentials": {"chain": {"login": "test", "password": "secret-value"}},
            "primary_admin": {"name": "Admin", "email": "admin@example.org", "phone": ""},
        },
    )
    assert company.status_code == 201, company.text
    company = company.json()
    access = client.post(
        BASE + "/companies/" + company["id"] + "/admin-access", json={"expected_version": 1}
    )
    assert access.status_code == 201, access.text
    return company, access.json()


def test_tenant_cookies_workspace_and_backup_restore(registry, tmp_path):
    client = client_for(registry)
    enter(client)
    company, access = prepare_company(client)
    tenant = client_for(registry)
    base = "/api/saas-tenant/tenant-one"
    response = tenant.post(
        base + "/auth/login",
        json={
            "username": "admin@example.org",
            "password": access["temporary_password"],
        },
    )
    assert response.status_code == 200
    secure_cookie(response)
    tenant.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert tenant.get(base + "/workspace").status_code == 403
    changed = tenant.post(
        base + "/auth/password",
        json={
            "current_password": access["temporary_password"],
            "new_password": "new-tenant-password",
        },
    )
    assert changed.status_code == 200, changed.text
    secure_cookie(changed)
    tenant.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
    assert tenant.get(base + "/workspace").json()["mode"] == "production"
    root, dist = registry
    backup = tmp_path / "backup"
    snapshot_registry(root, backup, clear_sessions=True)
    with (
        sqlite3.connect(root / "registry.sqlite3") as source,
        sqlite3.connect(backup / "registry.sqlite3") as copy,
    ):
        assert source.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert source.execute("SELECT COUNT(*) FROM tenant_sessions").fetchone()[0] == 1
        for table in ("sessions", "tenant_sessions"):
            assert copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in (
            "owners",
            "companies",
            "events",
            "connections",
            "tenant_admins",
            "tenant_events",
        ):
            assert (
                source.execute(f"SELECT * FROM {table}").fetchall()
                == copy.execute(f"SELECT * FROM {table}").fetchall()
            )
    restored = tmp_path / "restored"
    regular_backup = tmp_path / "regular-backup"
    snapshot_registry(root, regular_backup)
    restore_registry(regular_backup, restored)
    with sqlite3.connect(restored / "registry.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tenant_sessions").fetchone()[0] == 0
    assert (root / "credentials.key").read_bytes() == (restored / "credentials.key").read_bytes()
    reopened = Repository(restored)
    assert reopened.get(company["id"])["slug"] == "tenant-one"
    assert reopened.check_inputs(company["id"], "chain", 2)[2] == "secret-value"
    assert client.get(BASE + "/auth/me").status_code == 200
    assert tenant.get(base + "/auth/me").status_code == 200
    secure_cookie(tenant.post(base + "/auth/logout"))
    with pytest.raises(FileExistsError):
        restore_registry(backup, restored)


def test_missing_wrong_key_and_uninitialized_storage_fail_closed(registry, tmp_path):
    client = client_for(registry)
    enter(client)
    prepare_company(client)
    root, dist = registry
    (root / "credentials.key").write_bytes(Fernet.generate_key())
    with pytest.raises(ValueError, match="decrypt"):
        validate_registry(root)
    (root / "credentials.key").unlink()
    with pytest.raises(ValueError):
        validate_registry(root)
    assert not (root / "credentials.key").exists()
    empty = tmp_path / "uninitialized"
    Repository(empty)
    with pytest.raises(ValueError):
        create_app(empty, dist, ORIGIN, "production")


@pytest.mark.parametrize("socket_location", ["data", "static", "relative"])
def test_cli_rejects_socket_inside_data_static_or_relative(registry, monkeypatch, socket_location):
    import sys

    from app.saas_admin.__main__ import main

    root, dist = registry
    socket = {"data": root / "app.sock", "static": dist / "app.sock", "relative": "app.sock"}[
        socket_location
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "saas-admin",
            "serve",
            "--mode",
            "production",
            "--origin",
            ORIGIN,
            "--data-dir",
            str(root),
            "--dist-dir",
            str(dist),
            "--uds",
            str(socket),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_cli_uses_unix_socket_without_forwarded_trust(registry, tmp_path, monkeypatch):
    import sys

    import uvicorn

    from app.saas_admin.__main__ import main

    root, dist = registry
    monkeypatch.setattr("app.saas_admin.server.create_app", lambda *args: object())
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    socket = str(tmp_path / "run" / "saas.sock")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "saas-admin",
            "serve",
            "--mode",
            "production",
            "--origin",
            ORIGIN,
            "--data-dir",
            str(root),
            "--dist-dir",
            str(dist),
            "--uds",
            socket,
        ],
    )
    main()
    assert captured == {"uds": socket, "proxy_headers": False, "access_log": False}
