import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.models import CompanyWrite
from app.saas_admin.repository import Problem, Repository
from app.saas_admin.server import BASE, create_app
from app.saas_admin.tenant_routes import TENANT_COOKIE

ORIGIN = "http://127.0.0.1:8210"


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / "data")
    repo = app.state.repository
    repo.bootstrap("owner", "owner-password-123456", "Owner")
    owner = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    auth = owner.post(BASE + "/auth/login", json={
        "username": "owner", "password": "owner-password-123456",
    }).json()
    owner.headers["X-CSRF-Token"] = auth["csrf_token"]
    company = owner.post(BASE + "/companies", json={
        "name": "Private tenant", "slug": "tenant-one", "notes": "OWNER PRIVATE",
        "primary_admin": {"name": "Admin", "email": "ADMIN@example.org", "phone": "PRIVATE"},
    }).json()
    path = BASE + "/companies/" + company["id"]
    tenant = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    return repo, owner, tenant, company, path


def provision(owner, path, version=1):
    response = owner.post(path + "/admin-access", json={"expected_version": version})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def enter(tenant, access, slug="tenant-one"):
    base = "/api/saas-tenant/" + slug
    response = tenant.post(base + "/auth/login", json={
        "username": access["admin"]["username"], "password": access["temporary_password"],
    })
    assert response.status_code == 200, response.text
    tenant.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return base, response.json()


def change(tenant, base, old):
    response = tenant.post(base + "/auth/password", json={
        "current_password": old, "new_password": "new-permanent-password",
    })
    assert response.status_code == 200, response.text
    tenant.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


def test_explicit_create_change_scope_and_snapshot(setup):
    repo, owner, tenant, company, path = setup
    assert owner.get(path + "/admin-access").json()["exists"] is False
    access = provision(owner, path)
    assert access["admin"]["username"] == "admin@example.org"
    assert "temporary_password" not in owner.get(path + "/admin-access").text
    assert owner.post(path + "/admin-access", json={"expected_version": 1}).status_code == 409
    base, session = enter(tenant, access)
    assert session["must_change_password"]
    assert tenant.get(base + "/workspace").json()["detail"]["code"] == "password_change_required"
    assert tenant.get(BASE + "/companies").status_code == 401
    assert owner.get(base + "/auth/me").status_code == 401
    old_token = tenant.cookies.get(TENANT_COOKIE)
    assert not change(tenant, base, access["temporary_password"])["must_change_password"]
    assert tenant.cookies.get(TENANT_COOKIE) != old_token
    with pytest.raises(Problem):
        repo.tenant_session(old_token, "tenant-one")
    workspace = tenant.get(base + "/workspace").json()
    assert workspace["company"]["id"] == company["id"]
    assert set(workspace["company"]) == {"id", "name", "slug", "modules"}
    assert "PRIVATE" not in json.dumps(workspace)
    assert workspace["business_modules_ready"] is False
    owner.post(BASE + "/companies", json={"name": "Other", "slug": "tenant-two"})
    assert tenant.get("/api/saas-tenant/tenant-two/workspace").status_code == 401
    assert tenant.get("/api/saas-tenant/tenant-two/auth/me").status_code == 401
    assert owner.patch(path, json={"expected_version": 2, "primary_admin": {
        "name": "New", "email": "new@example.org", "phone": "",
    }}).status_code == 200
    assert owner.get(path + "/admin-access").json()["admin"]["username"] == "admin@example.org"
    with repo.connect() as db:
        assert db.execute("SELECT count(*) FROM tenant_events").fetchone()[0] == 1
        assert access["temporary_password"] not in str([tuple(r) for r in db.execute(
            "SELECT * FROM tenant_admins"
        )])


def test_reset_revokes_and_expiry(setup):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    change(tenant, base, access["temporary_password"])
    reset = owner.post(path + "/admin-access/reset", json={"expected_version": 2}).json()
    assert reset["company_version"] == 3
    assert reset["temporary_password"] != access["temporary_password"]
    assert tenant.get(base + "/auth/me").status_code == 401
    assert tenant.post(base + "/auth/login", json={
        "username": "admin@example.org", "password": "new-permanent-password",
    }).status_code == 401
    enter(tenant, reset)
    with repo.connect(True) as db:
        db.execute("UPDATE tenant_admins SET temporary_expires=1")
    assert tenant.get(base + "/auth/me").status_code == 401
    assert tenant.post(base + "/auth/login", json={
        "username": "admin@example.org", "password": reset["temporary_password"],
    }).status_code == 401
    assert owner.get(path + "/admin-access").json()["admin"]["status"] == "expired"


@pytest.mark.parametrize("action", ["suspend", "archive", "slug"])
def test_company_access_revocation(setup, action):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    if action == "archive":
        assert owner.delete(path + "?expected_version=2").status_code == 204
    else:
        values = {"status": "suspended"} if action == "suspend" else {"slug": "tenant-renamed"}
        assert owner.patch(path, json={"expected_version": 2, **values}).status_code == 200
    assert tenant.get(base + "/auth/me").status_code == 401
    with repo.connect() as db:
        assert db.execute("SELECT count(*) FROM tenant_sessions").fetchone()[0] == 0
    if action == "suspend":
        owner.patch(path, json={"expected_version": 3, "status": "active"})
        assert tenant.get(base + "/auth/me").status_code == 401


def test_boundary_csrf_password_policy_and_throttle(setup):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    payload = {"current_password": access["temporary_password"], "new_password": "new-password"}
    assert tenant.post(base + "/auth/password", json=payload,
                       headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert tenant.post(base + "/auth/password", json=payload,
                       headers={"Origin": "https://evil.org"}).status_code == 403
    assert tenant.get(base + "/auth/me", headers={"Host": "evil.org"}).status_code == 403
    assert tenant.post(base + "/auth/password", json={
        **payload, "new_password": "short",
    }).status_code == 422
    for _ in range(10):
        assert tenant.post(base + "/auth/password", json={
            **payload, "current_password": "bad",
        }).status_code == 401
    assert tenant.post(base + "/auth/password", json=payload).status_code == 429


def test_atomic_audit_and_provision_race(setup):
    repo, owner, tenant, company, path = setup
    actor = owner.get(BASE + "/auth/me").json()["user"]
    with repo.connect(True) as db:
        db.execute("CREATE TRIGGER deny_access BEFORE INSERT ON events "
                   "WHEN NEW.action='admin_access_created' BEGIN SELECT RAISE(ABORT,'deny'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.provision_admin(company["id"], 1, actor)
    assert not repo.admin_access(company["id"])["exists"]
    assert repo.get(company["id"])["version"] == 1
    with repo.connect(True) as db:
        db.execute("DROP TRIGGER deny_access")

    def attempt(_):
        try:
            repo.provision_admin(company["id"], 1, actor)
            return "created"
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["created", "version_conflict"]


def test_migration_preserves_existing_company_and_ciphertext(setup):
    repo, owner, tenant, company, path = setup
    actor = owner.get(BASE + "/auth/me").json()["user"]
    values = CompanyWrite(name="With credentials", slug="with-credentials",
                          chain_url="https://iiko.example.org").model_dump(mode="json")
    item = repo.save(values, actor, credentials={"chain": {"login": "test", "password": "secret"}})
    with repo.connect(True) as db:
        before = db.execute("SELECT ciphertext FROM connections").fetchone()[0]
        db.execute("DROP TABLE tenant_events")
        db.execute("DROP TABLE tenant_sessions")
        db.execute("DROP TABLE tenant_admins")
        db.execute("PRAGMA user_version=2")
    reopened = Repository(repo.path.parent)
    assert reopened.get(item["id"])["name"] == "With credentials"
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT ciphertext FROM connections").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM tenant_admins").fetchone()[0] == 0


def test_password_audit_rollback_and_eight_character_minimum(setup):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    token = tenant.cookies.get(TENANT_COOKIE)
    csrf = tenant.headers["X-CSRF-Token"]
    with repo.connect(True) as db:
        db.execute("CREATE TRIGGER deny_password BEFORE INSERT ON tenant_events "
                   "BEGIN SELECT RAISE(ABORT,'deny'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.tenant_password(token, "tenant-one", access["temporary_password"],
                             "abcdefgh", csrf, "testclient")
    assert repo.tenant_session(token, "tenant-one")["must_change_password"]
    with repo.connect(True) as db:
        db.execute("DROP TRIGGER deny_password")
    response = tenant.post(base + "/auth/password", json={
        "current_password": access["temporary_password"], "new_password": "abcdefgh",
    })
    assert response.status_code == 200
    assert not response.json()["must_change_password"]


def test_tenant_login_throttle_and_cookie_scope(setup):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    cookie = next(iter(tenant.cookies.jar))
    assert cookie.path == "/api/saas-tenant"
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.get_nonstandard_attr("SameSite") == "strict"
    for _ in range(10):
        response = tenant.post(base + "/auth/login", json={
            "username": "missing@example.org", "password": "bad",
        })
        assert response.status_code == 401
    assert tenant.post(base + "/auth/login", json={
        "username": access["admin"]["username"], "password": access["temporary_password"],
    }).status_code == 429
    assert owner.get(BASE + "/auth/me").status_code == 200


def test_malformed_csrf_fails_closed(setup):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    # Raw Latin-1 header bytes are valid ASGI input but not valid ASCII tokens.
    assert tenant.post(base + "/auth/logout", headers={
        b"x-csrf-token": b"\xff",
    }).status_code == 403
    assert owner.post(BASE + "/auth/logout", headers={
        b"x-csrf-token": b"\xff",
    }).status_code == 403
    with pytest.raises(Problem) as exc:
        repo.tenant_password(tenant.cookies.get(TENANT_COOKIE), "tenant-one",
                             access["temporary_password"], "newpassword", "\xff", "testclient")
    assert exc.value.status == 403


@pytest.mark.parametrize("field", ["username", "password"])
def test_invalid_unicode_login_rejected(setup, field):
    repo, owner, tenant, company, path = setup
    for route in [BASE + "/auth/login", "/api/saas-tenant/tenant-one/auth/login"]:
        payload = {"username": "admin@example.org", "password": "password123", field: "\ud800"}
        response = tenant.post(route, content=json.dumps(payload),
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 422
        assert "\\ud800" not in response.text


@pytest.mark.parametrize("field", ["current_password", "new_password"])
def test_invalid_unicode_password_change_rejected(setup, field):
    repo, owner, tenant, company, path = setup
    access = provision(owner, path)
    base, _ = enter(tenant, access)
    payload = {"current_password": access["temporary_password"],
               "new_password": "password123", field: "password\ud800"}
    response = tenant.post(base + "/auth/password", content=json.dumps(payload),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "\\ud800" not in response.text
    assert tenant.get(base + "/auth/me").json()["must_change_password"]
