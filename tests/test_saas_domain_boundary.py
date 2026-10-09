"""Host selection must never expose a platform API or another tenant."""

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.server import create_app
from app.saas_admin.vault import Vault


class DomainRepository:
    def company_for_domain(self, host):
        if host == "tenant.example.org":
            return {"id": "test-company", "name": "Client", "slug": "client", "notes": "SECRET"}
        return None


@pytest.fixture
def client(tmp_path):
    data, dist = tmp_path / "data", tmp_path / "dist"
    data.mkdir(mode=0o700)
    Vault(data)
    (dist / "assets").mkdir(parents=True)
    (dist / "saas-admin.html").write_text("<html>tenant application</html>")
    app = create_app(
        data, dist, "https://rc.example.org", "production", repository=DomainRepository()
    )
    with TestClient(app, base_url="https://tenant.example.org") as value:
        yield value


def test_customer_context_discloses_only_branding(client):
    reply = client.get("/api/saas-context")
    assert reply.status_code == 200
    assert reply.json() == {
        "surface": "tenant",
        "platform_origin": "https://rc.example.org",
        "company": {"id": "test-company", "name": "Client", "slug": "client"},
    }
    assert client.get("/").status_code == 200
    assert "access-control-allow-origin" not in reply.headers


@pytest.mark.parametrize(
    "path",
    [
        "/api/saas-admin/companies",
        "/api/saas-admin/auth/login",
        "/api/saas-tenant/other/auth/me",
        "/tenant/other",
    ],
)
def test_customer_host_never_serves_owner_or_other_company(client, path):
    assert client.get(path).status_code == 403


def test_exact_host_and_origin_with_no_forwarded_override(client):
    assert (
        client.get("/api/saas-context", headers={"host": "unknown.example.org"}).status_code == 403
    )
    reply = client.get("/api/saas-context", headers={"x-forwarded-host": "rc.example.org"})
    assert reply.json()["surface"] == "tenant"
    for origin in ["https://rc.example.org", "https://other.example.org", "null"]:
        reply = client.post(
            "/api/saas-tenant/client/auth/login", json={}, headers={"Origin": origin}
        )
        assert reply.status_code == 403
    reply = client.post(
        "/api/saas-tenant/client/auth/login",
        json={},
        headers={"Origin": "https://tenant.example.org"},
    )
    assert reply.status_code == 422


def test_platform_context_remains_separate(client):
    reply = client.get("/api/saas-context", headers={"host": "rc.example.org"})
    assert reply.json() == {"surface": "platform", "company": None, "platform_origin": "https://rc.example.org"}
