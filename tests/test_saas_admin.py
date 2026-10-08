import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.models import CompanyWrite
from app.saas_admin.repository import Repository
from app.saas_admin.server import BASE, COOKIE, create_app

ORIGIN = "http://127.0.0.1:8210"
PASSWORD = "long-test-password-12345"


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data")
    app.state.repository.bootstrap("owner", PASSWORD, "Owner")
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["Origin"] = ORIGIN
        yield client


def login(client):
    response = client.post(BASE + "/auth/login", json={"username": "owner", "password": PASSWORD})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def company(**kwargs):
    return {"name": "Example company", "slug": "company-one", **kwargs}


def test_auth_boundary(client):
    assert client.get(BASE + "/health").json() == {"status": "ok", "mode": "local"}
    assert client.get(BASE + "/companies").status_code == 401
    client.headers.pop("Origin")
    assert (
        client.post(
            BASE + "/auth/login", json={"username": "owner", "password": PASSWORD}
        ).status_code
        == 403
    )
    client.headers["Origin"] = ORIGIN
    response = login(client)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get(BASE + "/auth/me").json()["user"]["username"] == "owner"
    client.headers.pop("X-CSRF-Token")
    assert client.post(BASE + "/companies", json=company()).status_code == 403
    login(client)
    assert client.post(BASE + "/auth/logout").status_code == 204
    assert client.get(BASE + "/companies").status_code == 401
    assert client.get(BASE + "/health", headers={"Host": "evil.com"}).status_code == 403


def test_crud_audit_versions_archive_and_persistence(client):
    login(client)
    assert client.get(BASE + "/companies").json()["total"] == 0
    response = client.post(BASE + "/companies", json=company(domain="https://EXAMPLE.COM./"))
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["domain"] == "example.com"
    assert item["version"] == 1
    path = BASE + "/companies/" + item["id"]
    assert client.patch(path, json={"expected_version": 1, "name": "Renamed"}).status_code == 200
    assert client.patch(path, json={"expected_version": 1, "name": "Stale"}).status_code == 409
    assert client.patch(path, json={"name": "No version"}).status_code == 422
    assert client.get(BASE + "/companies?q=Renamed").json()["total"] == 1
    assert client.get(path + "/events").json()["total"] == 2
    repo = Repository(client.app.state.repository.path.parent)
    assert repo.get(item["id"])["name"] == "Renamed"
    assert client.delete(path + "?expected_version=1").status_code == 409
    assert client.delete(path + "?expected_version=2").status_code == 204
    assert client.get(path).status_code == 404
    assert client.get(BASE + "/companies").json()["total"] == 0
    events = client.get(path + "/events").json()
    assert events["total"] == 3
    assert events["items"][0]["action"] == "archived"
    assert client.post(BASE + "/companies", json=company()).status_code == 409
    assert (
        client.post(
            BASE + "/companies", json=company(slug="different", domain="example.com")
        ).status_code
        == 409
    )
    with sqlite3.connect(repo.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM companies").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT body FROM companies").fetchone()[0])["archived_at"]


@pytest.mark.parametrize(
    "domain",
    [
        "localhost",
        "127.0.0.1",
        "foo.local",
        "https://user@foo.com",
        "foo.com/path",
        "foo.com?q=1",
        "https://foo.com:443",
        "foo.com/#frag",
        "http://foo.com",
    ],
)
def test_domain_rejected(client, domain):
    login(client)
    assert client.post(BASE + "/companies", json=company(domain=domain)).status_code == 422


def test_contract_validation(client):
    login(client)
    for fields in [
        {"status": "healthy"},
        {"slug": "a"},
        {"password": "secret"},
        {"chain_url": "https://foo.com/?password=a"},
        {"subscription": {"end_date": "2026-01-01"}},
        {"subscription": {"start_date": "2026-02-01", "end_date": "2026-01-01"}},
        {"modules": {"analytics": "true"}},
    ]:
        assert client.post(BASE + "/companies", json=company(**fields)).status_code == 422
    assert CompanyWrite(**company(domain="ПРИМЕР.РФ.")).domain == "xn--e1afmkfd.xn--p1ai"
    assert client.get(BASE + "/companies?limit=1000").status_code == 422


def test_throttle_and_hashed_sessions(client):
    login(client)
    token = client.cookies.get(COOKIE)
    with sqlite3.connect(client.app.state.repository.path) as db:
        stored = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert stored != token and len(stored) == 64
    for _ in range(10):
        assert (
            client.post(
                BASE + "/auth/login", json={"username": "unknown", "password": "bad"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            BASE + "/auth/login", json={"username": "owner", "password": PASSWORD}
        ).status_code
        == 429
    )


def test_production_refused(tmp_path):
    with pytest.raises(ValueError, match="Production requires"):
        create_app(tmp_path, mode="production")
    assert not (tmp_path / "registry.sqlite3").exists()


def test_static_assets(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "saas-admin.html").write_text("<html>owner</html>")
    (dist / "index.html").write_text("<html>tenant</html>")
    with TestClient(create_app(tmp_path / "db", dist), base_url=ORIGIN) as client:
        assert "owner" in client.get("/").text
        assert "owner" in client.get("/companies/123").text
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/missing").status_code == 404


def test_atomic_audit_failure_rolls_back(client):
    login(client)
    repo = client.app.state.repository
    with repo.connect(True) as db:
        db.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN "
            "SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    session = repo.session(client.cookies.get(COOKIE))
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(CompanyWrite(**company()).model_dump(mode="json"), session["user"])
    assert repo.listing("", None, 50, 0)["total"] == 0


def test_concurrent_writes_only_one_wins(client):
    from concurrent.futures import ThreadPoolExecutor

    from app.saas_admin.repository import Problem

    login(client)
    repo = client.app.state.repository
    actor = repo.session(client.cookies.get(COOKIE))["user"]
    values = CompanyWrite(**company()).model_dump(mode="json")
    item = repo.save(values, actor)

    def update(name):
        try:
            return repo.save({**values, "name": name}, actor, item["id"], 1)["version"]
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["One", "Two"]))
    assert sorted(str(v) for v in results) == ["2", "version_conflict"]
    assert repo.events(item["id"], 50, 0)["total"] == 2


def test_cyrillic_search_and_expiry(client):
    login(client)
    assert client.post(BASE + "/companies", json=company(name="Чайка")).status_code == 201
    assert client.get(BASE + "/companies?q=чайка").json()["total"] == 1
    with client.app.state.repository.connect(True) as db:
        db.execute("UPDATE sessions SET expires=0")
    assert client.get(BASE + "/auth/me").status_code == 401


def test_body_limit(client):
    login(client)
    response = client.post(BASE + "/companies", content=iter([b"x" * 64001, b"x" * 64001]))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "body_too_large"


@pytest.mark.parametrize("value", ["https://foo.com:0", "https://@foo.com", "https://:@foo.com"])
@pytest.mark.parametrize("field", ["domain", "chain_url"])
def test_url_authority_edge_cases_rejected(client, value, field):
    login(client)
    response = client.post(BASE + "/companies", json=company(**{field: value}))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_error"


def test_spa_fallback_cannot_follow_external_symlink(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private-secret-marker")
    (dist / "saas-admin.html").symlink_to(outside)
    with TestClient(create_app(tmp_path / "db", dist), base_url=ORIGIN) as client:
        for path in ["/", "/companies/123", "/saas-admin.html"]:
            response = client.get(path)
            assert response.status_code == 404
            assert "private-secret-marker" not in response.text


def test_assets_cannot_traverse_into_other_dist_files(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("private-other-entry")
    (dist / "assets" / "app.js").write_text("safe-asset")
    with TestClient(create_app(tmp_path / "db", dist), base_url=ORIGIN) as client:
        assert client.get("/assets/app.js").text == "safe-asset"
        response = client.get("/assets/%2e%2e/index.html")
        assert response.status_code == 404
        assert "private-other-entry" not in response.text


@pytest.mark.parametrize(
    "layout", ["data_inside_dist", "dist_inside_data", "assets_symlink", "nested_symlink"]
)
def test_data_static_overlap_rejected_before_database_creation(tmp_path, layout):
    dist, data = tmp_path / "dist", tmp_path / "data"
    if layout == "data_inside_dist":
        data = dist / "data"
    elif layout == "dist_inside_data":
        dist = data / "dist"
    dist.mkdir(parents=True)
    if layout == "assets_symlink":
        (dist / "assets").symlink_to(data, target_is_directory=True)
    elif layout == "nested_symlink":
        (dist / "assets").mkdir()
        (dist / "assets" / "nested").symlink_to(data, target_is_directory=True)
    with pytest.raises(ValueError):
        create_app(data, dist)
    assert not (data / "registry.sqlite3").exists()


def test_domain_search_normalized(client):
    login(client)
    assert client.post(BASE + "/companies", json=company(domain="пример.рф")).status_code == 201
    for query in ["ПРИМЕР.РФ", "https://ПРИМЕР.РФ./", "xn--e1afmkfd.xn--p1ai"]:
        result = client.get(BASE + "/companies", params={"q": query})
        assert result.json()["total"] == 1
