"""The static Apps frontend gets a narrowly scoped, credentialed sibling API."""

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.repository import Problem
from app.saas_admin.server import create_app
from app.saas_admin.vault import Vault

ORIGIN = "https://tenant.example.org"
BASE = "/api/saas-tenant/client"


class DomainRepository:
    def __init__(self):
        self.domains = {
            "tenant.example.org": {"id": "one", "name": "Client", "slug": "client"},
            "other.example.org": {"id": "two", "name": "Other", "slug": "other"},
        }
        self.membership = True
        self.calls = []

    def company_for_domain(self, host):
        self.calls.append(host)
        return self.domains.get(host)

    def tenant_login(self, slug, username, password, remote, previous):
        if username != "admin" or password != "test-password" or slug != "client":
            raise Problem(401, "invalid_credentials", "Invalid credentials")
        return "test-session", {"csrf_token": "test-csrf", "must_change_password": False}

    def tenant_session(self, token, slug):
        if token != "test-session" or slug != "client" or not self.membership:
            raise Problem(401, "unauthorized", "Unauthorized")
        return {"csrf_token": "test-csrf", "must_change_password": False}

    def tenant_logout(self, token):
        assert token == "test-session"


@pytest.fixture
def client(tmp_path):
    data, dist = tmp_path / "data", tmp_path / "dist"
    data.mkdir(mode=0o700)
    Vault(data)
    (dist / "assets").mkdir(parents=True)
    (dist / "saas-admin.html").write_text("<html>application</html>")
    app = create_app(
        data, dist, "https://rc.example.org", "production", repository=DomainRepository()
    )
    with TestClient(app, base_url="https://api.tenant.example.org") as value:
        yield value


def cors(reply):
    assert reply.headers["access-control-allow-origin"] == ORIGIN
    assert reply.headers["access-control-allow-credentials"] == "true"
    assert "origin" in reply.headers["vary"].lower()
    assert reply.headers["cache-control"] == "no-store"


def login(client):
    reply = client.post(
        BASE + "/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "admin", "password": "test-password"},
    )
    assert reply.status_code == 200
    cors(reply)
    cookie = reply.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie and "Path=/" in cookie
    return reply


def test_context_uses_exact_registry_and_ignores_forwarded_host(client):
    reply = client.get(
        "/api/saas-context",
        headers={"Origin": ORIGIN, "X-Forwarded-Host": "api.other.example.org"},
    )
    assert reply.status_code == 200
    assert reply.json() == {
        "surface": "tenant",
        "platform_origin": "https://rc.example.org",
        "company": {"id": "one", "name": "Client", "slug": "client"},
    }
    assert client.app.state.repository.calls == ["api.tenant.example.org", "tenant.example.org"]
    cors(reply)


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://other.example.org",
        ORIGIN + "/",
        "https://api.tenant.example.org",
        ORIGIN + ":443",
    ],
)
def test_every_api_read_requires_exact_frontend_origin(client, origin):
    reply = client.get("/api/saas-context", headers={"Origin": origin} if origin else {})
    assert reply.status_code == 403
    assert "access-control-allow-origin" not in reply.headers


@pytest.mark.parametrize(
    "headers",
    [
        [("Origin", ORIGIN), ("Origin", ORIGIN)],
        [("Origin", ORIGIN), ("Origin", "https://other.example.org")],
        [("Origin", ORIGIN + ", https://other.example.org")],
        [("Origin", ORIGIN), ("Host", "api.tenant.example.org"), ("Host", "rc.example.org")],
    ],
)
def test_duplicate_or_multi_origins_and_hosts_fail_closed(client, headers):
    reply = client.get("/api/saas-context", headers=headers)
    assert reply.status_code == 403
    assert "access-control-allow-origin" not in reply.headers


@pytest.mark.parametrize(
    "host",
    [
        "api.unknown.example.org",
        "api.tenant.example.org:443",
        "API.tenant.example.org",
        "api.rc.example.org",
    ],
)
def test_unknown_or_noncanonical_api_host_is_denied(client, host):
    reply = client.get("/api/saas-context", headers={"Origin": ORIGIN, "Host": host})
    assert reply.status_code == 403
    assert "access-control-allow-origin" not in reply.headers


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/tenant/client",
        "/api/saas-admin/health",
        "/api/saas-admin/auth/login",
        "/api/saas-tenant/other/auth/me",
        "/api/saas-tenant/client-evil/auth/me",
    ],
)
def test_api_host_cannot_serve_platform_static_or_other_tenant(client, path):
    reply = client.get(path, headers={"Origin": ORIGIN})
    assert reply.status_code == 403
    cors(reply)


def test_wrong_host_even_with_valid_session_and_origin(client):
    login(client)
    reply = client.get(
        BASE + "/auth/me",
        headers={"Origin": "https://other.example.org", "Host": "api.other.example.org"},
    )
    assert reply.status_code == 403
    assert reply.json()["detail"]["code"] == "tenant_boundary"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_preflight_is_unauthenticated_and_minimal(client, method):
    reply = client.options(
        BASE + "/auth/login",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "Content-Type, X-CSRF-Token",
        },
    )
    assert reply.status_code == 204
    cors(reply)
    assert reply.headers["access-control-allow-methods"] == "GET, POST"
    assert reply.headers["access-control-allow-headers"] == "content-type, x-csrf-token"
    assert "set-cookie" not in reply.headers


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"Access-Control-Request-Method": "DELETE"},
        {"Access-Control-Request-Method": "GET, POST"},
        {"Access-Control-Request-Method": "get"},
        {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
        {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,",
        },
        {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "*"},
    ],
)
def test_invalid_preflight_has_no_permission_headers(client, extra):
    reply = client.options(BASE + "/auth/login", headers={"Origin": ORIGIN, **extra})
    assert reply.status_code == 403
    cors(reply)
    assert "access-control-allow-methods" not in reply.headers
    assert "access-control-allow-headers" not in reply.headers


def test_preflight_cannot_override_domain_or_namespace(client):
    for host, origin, path in [
        ("api.unknown.example.org", ORIGIN, BASE + "/auth/login"),
        ("api.tenant.example.org", "https://other.example.org", BASE + "/auth/login"),
        ("api.tenant.example.org", ORIGIN, "/api/saas-admin/auth/login"),
        ("api.tenant.example.org", ORIGIN, "/api/saas-tenant/other/auth/login"),
    ]:
        reply = client.options(
            path, headers={"Host": host, "Origin": origin, "Access-Control-Request-Method": "POST"}
        )
        assert reply.status_code == 403
        assert "access-control-allow-methods" not in reply.headers


def test_duplicate_preflight_headers_are_denied(client):
    for duplicate in ("Access-Control-Request-Method", "Access-Control-Request-Headers"):
        value = "POST" if duplicate.endswith("Method") else "content-type"
        headers = [("Origin", ORIGIN), ("Access-Control-Request-Method", "POST")]
        if duplicate.endswith("Headers"):
            headers.append((duplicate, value))
        headers.append((duplicate, value))
        assert client.options(BASE + "/auth/login", headers=headers).status_code == 403


def test_csrf_and_membership_are_still_required(client):
    login(client)
    reply = client.get(BASE + "/auth/me", headers={"Origin": ORIGIN})
    assert reply.status_code == 200
    cors(reply)
    for token in (None, "wrong"):
        headers = {"Origin": ORIGIN}
        if token:
            headers["X-CSRF-Token"] = token
        reply = client.post(BASE + "/auth/logout", headers=headers)
        assert reply.status_code == 403
        assert reply.json()["detail"]["code"] == "csrf_failed"
        cors(reply)
    reply = client.post(
        BASE + "/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": "test-csrf"}
    )
    assert reply.status_code == 204
    cors(reply)
    login(client)
    client.app.state.repository.membership = False
    reply = client.get(BASE + "/auth/me", headers={"Origin": ORIGIN})
    assert reply.status_code == 401
    cors(reply)


def test_domain_revocation_removes_context_and_preflight(client):
    assert client.get("/api/saas-context", headers={"Origin": ORIGIN}).status_code == 200
    del client.app.state.repository.domains["tenant.example.org"]
    for method in ("GET", "OPTIONS", "POST"):
        reply = client.request(
            method,
            "/api/saas-context",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
        )
        assert reply.status_code == 403
        assert "access-control-allow-origin" not in reply.headers


def test_validation_unauthorized_missing_route_and_size_errors_include_cors(client):
    replies = [
        client.post(BASE + "/auth/login", headers={"Origin": ORIGIN}, json={}),
        client.get(BASE + "/auth/me", headers={"Origin": ORIGIN}),
        client.get(BASE + "/missing", headers={"Origin": ORIGIN}),
        client.post(BASE + "/auth/login", headers={"Origin": ORIGIN}, content=b"x" * 128001),
    ]
    assert [reply.status_code for reply in replies] == [422, 401, 404, 422]
    for reply in replies:
        cors(reply)


@pytest.mark.parametrize("method", ["HEAD", "PUT", "PATCH", "DELETE"])
def test_actual_methods_are_restricted(client, method):
    reply = client.request(method, BASE + "/auth/login", headers={"Origin": ORIGIN})
    assert reply.status_code == 403
    cors(reply)


def test_cross_site_fetch_is_rejected_even_with_valid_origin(client):
    reply = client.get(
        "/api/saas-context", headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}
    )
    assert reply.status_code == 403


def test_legacy_same_origin_and_platform_stay_available(client):
    for host, surface in [("tenant.example.org", "tenant"), ("rc.example.org", "platform")]:
        reply = client.get("/api/saas-context", headers={"Host": host})
        assert reply.status_code == 200
        assert reply.json()["surface"] == surface
        assert "access-control-allow-origin" not in reply.headers
    assert (
        client.get("/api/saas-admin/health", headers={"Host": "rc.example.org"}).status_code == 200
    )


def test_unexpected_server_error_is_sanitized_and_keeps_cors(client, monkeypatch):
    def unavailable(token, slug):
        raise RuntimeError("private connection details")

    monkeypatch.setattr(client.app.state.repository, "tenant_session", unavailable)
    with TestClient(
        client.app, base_url="https://api.tenant.example.org", raise_server_exceptions=False
    ) as browser:
        reply = browser.get(BASE + "/auth/me", headers={"Origin": ORIGIN})
    assert reply.status_code == 500
    assert "private connection details" not in reply.text
    cors(reply)


def test_same_origin_login_cookie_session_and_csrf(client):
    with TestClient(client.app, base_url=ORIGIN) as site:
        context = site.get("/api/saas-context")
        assert context.status_code == 200
        assert "access-control-allow-origin" not in context.headers
        reply = site.post(
            BASE + "/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "admin", "password": "test-password"},
        )
        assert reply.status_code == 200
        cookie = reply.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Domain=" not in cookie
        assert site.get(BASE + "/auth/me").status_code == 200
        assert site.post(BASE + "/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
        assert (
            site.post(
                BASE + "/auth/logout",
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": "test-csrf",
                },
            ).status_code
            == 204
        )


@pytest.mark.parametrize(
    "host,slug", [("tenant.example.org", "client"), ("other.example.org", "other")]
)
def test_same_origin_exact_host_and_namespace(client, host, slug):
    with TestClient(client.app, base_url="https://" + host) as site:
        context = site.get("/api/saas-context", headers={"X-Forwarded-Host": "unknown.example.org"})
        assert context.status_code == 200
        assert context.json()["company"]["slug"] == slug
        assert site.get("/api/saas-tenant/foreign/auth/me").status_code == 403
        assert site.get("/api/saas-admin/auth/me").status_code == 403
        assert (
            site.get(
                "/api/saas-context", headers={"Origin": "https://evil.example.org"}
            ).status_code
            == 403
        )
        assert (
            site.get("/api/saas-context", headers={"Sec-Fetch-Site": "cross-site"}).status_code
            == 403
        )
        assert (
            site.get(
                "/api/saas-context",
                headers={"Host": "unknown.example.org", "X-Forwarded-Host": host},
            ).status_code
            == 403
        )


def test_registered_site_starting_api_is_not_mistaken_for_alias(client):
    client.app.state.repository.domains["api.custom.example.org"] = {
        "id": "three",
        "name": "API site",
        "slug": "api-site",
    }
    response = client.get("https://api.custom.example.org/api/saas-context")
    assert response.status_code == 200
    assert response.json()["company"]["slug"] == "api-site"
