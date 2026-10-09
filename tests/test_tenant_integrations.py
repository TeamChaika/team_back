"""Real registry and sessions, synthetic Auth: no provider requests or secrets."""

import json
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.company_module_settings import CompanyModuleSettings, IntegrationsWrite
from app.saas_admin.platform_sso import pkce_challenge
from app.saas_admin.server import create_app
from tests.test_saas_admin_postgres import OWNER_AUTH
from tests.test_saas_admin_postgres import database as database
from tests.test_saas_admin_postgres import imported as imported
from tests.test_saas_admin_postgres import pg_server as pg_server

TOKEN = "123456789:" + "s" * 32
KEY = "synthetic-provider-private-key"


@pytest.fixture
def tenant(imported, tmp_path):
    repo, _, owner, first, second, _ = imported
    repo.auth = SimpleNamespace(user=lambda token: {"id": OWNER_AUTH})
    tokens = dict(
        access_token="synthetic-access",
        refresh_token="synthetic-refresh",
        user_id=OWNER_AUTH,
        expires_at=9999999999,
    )
    with repo.connect(True) as db:
        first = repo._get(db, first["id"])
        first["status"] = "active"
        repo._write_company(db, first)
        second = repo._get(db, second["id"])
        second.update(status="active", domain="two.example.com")
        repo._write_company(db, second)
        row = db.execute(
            "UPDATE memberships SET auth_user_id=%s,must_change=false RETURNING *", (OWNER_AUTH,)
        ).fetchone()
        token, csrf = repo._issue(db, row, tokens, tenant=True)
        owner_row = db.execute("SELECT * FROM platform_memberships").fetchone()
        parent, _ = repo._issue(db, owner_row, tokens)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "saas-admin.html").write_text("<html>tenant application</html>")
    app = create_app(
        tmp_path / "registry",
        dist,
        repository=repo,
        mode="production",
        origin="https://rc.chaika.team",
    )
    with TestClient(app, base_url="https://api.one.example.com") as client:
        client.headers.update({"Origin": "https://one.example.com", "X-CSRF-Token": csrf})
        client.cookies.set("saas_tenant_session", token)
        yield repo, client, first, second, parent


def test_company_admin_secret_write_role_domain_csrf_and_version(tenant):
    repo, client, first, second, _ = tenant
    url = "/api/saas-tenant/company-one/integrations"
    current = client.get(url)
    assert current.status_code == 200
    assert "seller" not in current.json()
    assert "apply_status" not in current.json()
    assert client.get("/api/saas-tenant/company-one/auth/me").json()["can_manage_integrations"]
    body = dict(
        expected_version=current.json()["company_version"],
        expected_revision=current.json()["integrations_revision"],
        telegram=dict(username="company_bot", token=TOKEN),
        assistant=dict(provider="openai", key=KEY, model="own-model"),
    )
    assert client.post(url, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(url, json={**body, "seller": None}).status_code == 422
    result = client.post(url, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["company_version"] == first["version"]
    assert result.json()["integrations_revision"] == body["expected_revision"] + 1
    assert TOKEN not in result.text and KEY not in result.text
    assert client.post(url, json=body).status_code == 409
    assert client.get(url.replace("company-one", "company-two")).status_code == 403
    assert (
        client.get(
            url, headers={"Host": "api.two.example.com", "Origin": "https://two.example.com"}
        ).status_code
        == 403
    )
    assert client.get(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert CompanyModuleSettings(repo).integrations_revision(second["id"]) == 1
    with repo.connect(True) as db:
        raw = str(db.execute("SELECT * FROM company_module_settings").fetchall())
        audit = str(db.execute("SELECT * FROM tenant_events").fetchall())
        assert TOKEN not in raw + audit and KEY not in raw + audit
        db.execute("UPDATE memberships SET role='employee'")
    assert client.get(url).status_code == 403
    assert client.post(url, json=body).status_code == 403
    assert not client.get("/api/saas-tenant/company-one/auth/me").json()["can_manage_integrations"]


def test_owner_sso_and_revocation(tenant):
    repo, client, first, _, parent = tenant
    verifier, state, nonce = [secrets.token_urlsafe(32) for _ in range(3)]
    grant = repo.authorize_platform(parent, first["id"], state, nonce, pkce_challenge(verifier))
    token, session = repo.exchange_platform(
        grant["code"],
        verifier,
        state,
        nonce,
        "company-one",
        "https://one.example.com",
        "https://api.one.example.com",
    )
    client.cookies.set("saas_tenant_session", token)
    client.headers["X-CSRF-Token"] = session["csrf_token"]
    url = "/api/saas-tenant/company-one/integrations"
    current = client.get(url).json()
    response = client.post(
        url,
        json=dict(
            expected_version=current["company_version"],
            expected_revision=current["integrations_revision"],
            assistant=dict(key=KEY),
        ),
    )
    assert response.status_code == 200, response.text
    assert KEY not in json.dumps(repo.events(first["id"], 100, 0))
    repo.logout(parent)
    assert client.get(url).status_code == 401


def test_self_service_validation_rejects_unknown_and_empty():
    for values in [dict(seller=None), dict(), dict(telegram=None), dict(expected_revision=True)]:
        with pytest.raises(ValueError):
            IntegrationsWrite(**{**dict(expected_version=1, expected_revision=1), **values})


def test_preserve_clear_provider_rotation_and_revoked_member(tenant):
    repo, client, first, _, _ = tenant
    url = "/api/saas-tenant/company-one/integrations"

    def save(**groups):
        current = client.get(url).json()
        response = client.post(
            url,
            json={
                "expected_version": current["company_version"],
                "expected_revision": current["integrations_revision"],
                **groups,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    save(telegram=dict(username="company_bot", token=TOKEN), assistant=dict(key=KEY))
    result = save(telegram=dict(username="company_bot", token=""), assistant=dict(key=""))
    assert result["telegram"]["token_configured"] and result["assistant"]["key_configured"]
    result = save(telegram=dict(clear_token=True), assistant=dict(provider="openrouter"))
    assert not result["telegram"]["token_configured"]
    assert not result["assistant"]["key_configured"]
    assert repo.get(first["id"])["version"] == first["version"]
    with repo.connect(True) as db:
        db.execute("UPDATE memberships SET active=false")
    assert client.get(url).status_code == 401


def test_tenant_edit_rejects_stale_owner_form(tenant):
    from app.saas_admin.company_module_settings import ModuleSettingsWrite
    from app.saas_admin.repository import Problem

    repo, client, first, _, _ = tenant
    with repo.connect() as db:
        owner = db.execute("SELECT * FROM platform_memberships").fetchone()
    service = CompanyModuleSettings(repo)
    old = service.get(first["id"], owner)
    response = client.post(
        "/api/saas-tenant/company-one/integrations",
        json={
            "expected_version": old["company_version"],
            "expected_revision": old["integrations_revision"],
            "assistant": {"key": KEY, "model": "new-company-model"},
        },
    )
    assert response.status_code == 200
    for revision in (None, old["integrations_revision"]):
        with pytest.raises(Problem) as error:
            service.update(
                first["id"],
                ModuleSettingsWrite(
                    expected_version=old["company_version"],
                    expected_revision=revision,
                    assistant={"model": "stale-owner-model"},
                ),
                owner,
            )
        assert error.value.code == "version_conflict"
    current = service.get(first["id"], owner)
    assert current["assistant"]["model"] == "new-company-model"
    service.update(
        first["id"],
        ModuleSettingsWrite(
            expected_version=current["company_version"],
            expected_revision=current["integrations_revision"],
            assistant={"model": "owner-model"},
        ),
        owner,
    )
