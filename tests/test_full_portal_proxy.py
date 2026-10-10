"""Gateway/verifier contracts. Synthetic upstream; no real iiko/payment calls."""

from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.saas_admin.entitlements import FEATURES
from app.saas_admin.repository import Problem
from app.saas_admin.runtime_registry import ReadyRuntime
from app.saas_admin.server import create_app
from app.saas_admin.vault import Vault
from app.tenancy.actor import ActorContext
from app.tenancy.bootstrap import VerifierGrant, create_verifier_app


class Repo:
    def __init__(self):
        self.companies = {
            f"client{i}.example.org": {
                "id": str(UUID(int=i)),
                "name": f"Client {i}",
                "slug": f"client{i}",
                "version": 1,
                "status": "active",
                "modules": {"analytics": True},
                "subscription": {
                    "policy": "plans_v1",
                    "plan_id": "analytics",
                    "status": "active",
                    "start_date": "2020-01-01",
                },
            }
            for i in (1, 2)
        }
        self.revoked = False
        self.must_change = False

    def company_for_domain(self, host):
        return self.companies.get(host)

    def get(self, company_id):
        return next(c for c in self.companies.values() if c["id"] == company_id)

    def tenant_actor_session(self, token, company_id):
        if self.revoked or token != company_id:
            raise Problem(401, "unauthorized", "Unauthorized")
        return ActorContext(UUID(company_id), UUID(int=55), "platform_owner", "Owner"), {
            "csrf_token": "csrf",
            "must_change_password": self.must_change,
        }

    def authorize_platform_actor(self, company_id, auth_user_id):
        return ActorContext(UUID(company_id), UUID(auth_user_id), "platform_owner", "Owner")


class Registry:
    ready = True

    def feature_readiness(self, company):
        # Explicit synthetic capability evidence for transport-boundary tests.
        return getattr(
            self,
            "features",
            {feature: {"read": self.ready, "write": self.ready} for feature in FEATURES},
        )

    @contextmanager
    def payment_configuration_change(self, company):
        self.invalidated = company["id"]
        if hasattr(self, "features"):
            self.features["payments.create"] = {"read": True, "write": False}
        yield

    def resolve(self, company):
        if self.ready:
            return ReadyRuntime(
                company["id"], company["version"], f"/tmp/c_{UUID(company['id']).hex}/portal.sock"
            )


@pytest.fixture
def gateway(tmp_path):
    repo, registry = Repo(), Registry()
    data, dist = tmp_path / "data", tmp_path / "dist"
    data.mkdir(mode=0o700)
    Vault(data)
    (dist / "assets").mkdir(parents=True)
    (dist / "saas-admin.html").write_text("test")
    calls = []

    def transport(runtime):
        def handler(request):
            calls.append(request)
            return httpx.Response(
                getattr(registry, "upstream_status", 200),
                json=getattr(registry, "metadata", {"company": runtime.company_id}),
                headers=getattr(registry, "upstream_headers", {}),
            )

        return httpx.MockTransport(handler)

    app = create_app(
        data,
        dist,
        "https://rc.example.org",
        "production",
        repository=repo,
        runtime_registry=registry,
        proxy_transport_factory=transport,
    )
    with TestClient(app) as client:
        yield client, repo, registry, calls


def request(client, index, path="/api/overview", method="GET", **kwargs):
    return client.request(
        method,
        f"https://api.client{index}.example.org{path}",
        headers={
            "origin": f"https://client{index}.example.org",
            "cookie": f"saas_tenant_session={UUID(int=index)}",
            **kwargs.pop("headers", {}),
        },
        **kwargs,
    )


def test_two_company_routing_and_cross_company_denial(gateway):
    client, repo, registry, calls = gateway
    for index in (1, 2):
        assert request(client, index).json() == {"company": str(UUID(int=index))}
    assert (
        request(client, 2, headers={"cookie": f"saas_tenant_session={UUID(int=1)}"}).status_code
        == 401
    )
    assert len(calls) == 2
    assert request(client, 1, headers={"origin": "https://client2.example.org"}).status_code == 403
    assert request(client, 1, "/api/saas-admin/companies").status_code == 403
    assert request(client, 1, "/api/new-unreviewed-route").status_code == 403
    assert request(client, 1, "/api/deposits").status_code == 403
    repo.revoked = True
    assert request(client, 1).status_code == 401


def test_csrf_readiness_and_write_subscription(gateway):
    client, repo, registry, calls = gateway
    assert request(client, 1, "/api/status/sync/all/run", "POST", json={}).status_code == 403
    assert (
        request(
            client, 1, "/api/status/sync/all/run", "POST", json={}, headers={"x-csrf-token": "csrf"}
        ).status_code
        == 200
    )
    repo.companies["client1.example.org"]["subscription"]["end_date"] = "2020-02-01"
    assert (
        request(
            client, 1, "/api/status/sync/all/run", "POST", json={}, headers={"x-csrf-token": "csrf"}
        ).status_code
        == 403
    )
    assert request(client, 1).status_code == 200
    registry.ready = False
    assert request(client, 1).status_code == 503
    assert request(client, 1, "/api/saas-context").json()["full_dashboard_ready"] is False


def test_verifier_company_and_worker_scope():
    repo = Repo()
    secret = "s" * 40
    company = str(UUID(int=1))
    app = create_verifier_app(repo, [VerifierGrant(company, "documents-worker", secret)])
    with TestClient(app) as client:
        headers = {"authorization": "Bearer " + secret}
        assert (
            client.post(
                f"/verify/{company}/session", headers=headers, json={"token": company}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/verify/{UUID(int=2)}/owner",
                headers=headers,
                json={"auth_user_id": str(UUID(int=55))},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/verify/{company}/owner",
                headers=headers,
                json={"auth_user_id": str(UUID(int=55))},
            ).json()["actor"]["company_id"]
            == company
        )


def test_public_capability_routes_have_separate_boundary(gateway):
    client, repo, registry, calls = gateway
    deposit = str(UUID(int=44))
    repo.companies["client1.example.org"]["modules"]["deposits"] = True
    repo.companies["client1.example.org"]["subscription"]["plan_id"] = "full"
    repo.revoked = True  # Guest capability auth is performed by payment handler, not staff auth.
    base = "https://api.client1.example.org"
    assert (
        client.get(
            base + f"/api/guest-deposits/{deposit}?token=guest",
            headers={"origin": "https://client1.example.org"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            base + f"/api/guest-deposits/{deposit}/prepare",
            headers={"origin": "https://client1.example.org"},
            json={"token": "guest"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            base + f"/api/guest-deposits/{deposit}/prepare", json={"token": "guest"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            base + f"/api/payment-callbacks/{deposit}?token=callback",
            json={"operation_id": deposit},
        ).status_code
        == 200
    )
    assert client.post(base + "/api/payment-callbacks/not-a-uuid", json={}).status_code == 403
    assert (
        client.get(
            base + f"/api/payment-callbacks/{deposit}",
            headers={"origin": "https://client1.example.org"},
        ).status_code
        == 403
    )
    assert calls[-1].url.query == b"token=callback"
    assert calls[-1].headers.get("cookie") == "saas_tenant_session="


def test_feature_mapping_distinguishes_catalog_and_creation():
    from app.saas_admin.full_portal_proxy import route_features

    path = "/api/commercial-invoices/receipt/counterparties"
    assert route_features(path) == ("commercial.incoming",)
    assert route_features(path, "POST") == ("commercial.incoming", "commercial.counterparties")
    assert route_features("/api/profile/telegram/link", "POST") == (
        "profile.account",
        "notifications.telegram",
    )
    assert "documents.dispatch" in route_features("/api/documents/waybill/1/confirm", "POST")


def test_must_change_account_recovery_and_safe_cookie_rotation(gateway):
    client, repo, registry, calls = gateway
    repo.must_change = True
    opaque = "x" * 43
    registry.upstream_headers = [
        (
            "set-cookie",
            f"saas_tenant_session={opaque}; Path=/; Max-Age=28800; "
            "Secure; HttpOnly; SameSite=Strict",
        ),
        ("set-cookie", "chaika_access=must-not-leak; Path=/; Secure; HttpOnly"),
    ]
    assert request(client, 1).status_code == 403
    assert request(client, 1, "/api/me").status_code == 200
    for status in (200, 503):
        registry.upstream_status = status
        result = request(
            client, 1, "/api/profile/password", "POST", json={}, headers={"x-csrf-token": "csrf"}
        )
        assert result.status_code == status
        assert result.cookies.get("saas_tenant_session") == opaque
        assert "chaika_access" not in result.headers.get("set-cookie", "")
        assert "Domain=" not in result.headers["set-cookie"]
    registry.upstream_status = 200
    registry.upstream_headers = {
        "set-cookie": (
            f"saas_tenant_session={opaque}; Domain=.example.org; Path=/; "
            "Max-Age=28800; Secure; HttpOnly; SameSite=Strict"
        )
    }
    result = request(
        client, 1, "/api/profile/password", "POST", json={}, headers={"x-csrf-token": "csrf"}
    )
    assert "set-cookie" not in result.headers
    assert (
        request(
            client, 1, "/api/auth/logout", "POST", json={}, headers={"x-csrf-token": "csrf"}
        ).status_code
        == 200
    )


def test_patch_preflight_only_on_known_deposit_record(gateway):
    client, *_ = gateway
    headers = {
        "origin": "https://client1.example.org",
        "access-control-request-method": "PATCH",
        "access-control-request-headers": "content-type,x-csrf-token",
    }
    assert (
        client.options(
            f"https://api.client1.example.org/api/deposits/{UUID(int=8)}", headers=headers
        ).status_code
        == 204
    )
    assert (
        client.options("https://api.client1.example.org/api/overview", headers=headers).status_code
        == 403
    )
    assert (
        client.options(
            f"https://api.client1.example.org/api/deposits/{UUID(int=8)}",
            headers={**headers, "access-control-request-method": "DELETE"},
        ).status_code
        == 403
    )


def test_private_worker_feature_check_is_fresh_and_never_accepts_reconcile_claim():
    repo = Repo()
    company = str(UUID(int=1))
    repo.companies["client1.example.org"]["modules"]["documents"] = True
    repo.companies["client1.example.org"]["subscription"]["plan_id"] = "full"
    app = create_verifier_app(
        repo, [VerifierGrant(company, "documents-worker", "s" * 40)], runtime_registry=Registry()
    )
    with TestClient(app) as client:
        url = f"/verify/{company}/feature"
        headers = {"authorization": "Bearer " + "s" * 40}
        assert (
            client.post(
                url, headers=headers, json={"feature": "documents.dispatch", "operation": "create"}
            ).status_code
            == 200
        )
        repo.companies["client1.example.org"]["subscription"]["end_date"] = "2020-02-01"
        assert (
            client.post(
                url, headers=headers, json={"feature": "documents.dispatch", "operation": "create"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                url,
                headers=headers,
                json={
                    "feature": "documents.dispatch",
                    "operation": "reconcile",
                    "initiated_operation": True,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(url, headers=headers, json={"feature": "payments.create"}).status_code
            == 403
        )


def test_public_purchase_namespace_routes_to_commercial_feature(gateway):
    client, repo, registry, calls = gateway
    company = repo.companies["client1.example.org"]
    company["modules"]["commercial_invoices"] = True
    company["subscription"]["plan_id"] = "full"
    assert request(client, 1, "/api/commercial-invoices/purchase").status_code == 200
    company["subscription"]["overrides"] = {"commercial.incoming": {"mode": "deny"}}
    assert request(client, 1, "/api/commercial-invoices/purchase").status_code == 403


def test_recovery_public_exact_routes_still_require_own_origin(gateway):
    client, repo, registry, calls = gateway
    for method, path in [
        ("GET", "/api/auth/recovery/telegram"),
        ("POST", "/api/auth/recovery/reset"),
    ]:
        url = "https://api.client1.example.org" + path
        assert (
            client.request(
                method, url, headers={"origin": "https://client1.example.org"}
            ).status_code
            == 200
        )
        assert "saas_tenant_session=" == calls[-1].headers.get("cookie")
        assert client.request(method, url).status_code == 403
        assert (
            client.request(
                method, url, headers={"origin": "https://client2.example.org"}
            ).status_code
            == 403
        )
    assert (
        client.get(
            "https://api.client1.example.org/api/auth/recovery/reset",
            headers={"origin": "https://client1.example.org"},
        ).status_code
        == 403
    )


def test_verifier_recovery_is_company_bound_and_portal_only():
    company = str(UUID(int=1))
    calls = []

    class Accounts:
        def recover_password(self, company_id, token, password):
            calls.append((company_id, token, password))
            return {"status": "ok"}

    portal = VerifierGrant(company, "portal", "p" * 40)
    worker = VerifierGrant(company, "documents-worker", "w" * 40)
    app = create_verifier_app(Repo(), [portal, worker], company_accounts=Accounts())
    with TestClient(app) as client:
        payload = {"token": "r" * 43, "new_password": "new-password"}
        assert client.post(
            f"/verify/{company}/recovery",
            json=payload,
            headers={"authorization": "Bearer " + portal.secret},
        ).json() == {"status": "ok"}
        assert (
            client.post(
                f"/verify/{company}/recovery",
                json=payload,
                headers={"authorization": "Bearer " + worker.secret},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/verify/{UUID(int=2)}/recovery",
                json=payload,
                headers={"authorization": "Bearer " + portal.secret},
            ).status_code
            == 403
        )
    assert calls == [(company, payload["token"], payload["new_password"])]


def test_existing_binding_keeps_history_but_not_new_writes(gateway):
    client, repo, registry, calls = gateway
    accepted = registry.resolve(repo.get(str(UUID(int=1))))
    registry.resolve_existing = lambda company: (
        accepted if company["id"] == accepted.company_id else None
    )
    registry.ready = False
    company = repo.companies["client1.example.org"]
    company["version"] += 1
    company["subscription"]["end_date"] = "2020-01-02"
    context = request(client, 1, "/api/saas-context").json()
    assert context["full_dashboard_ready"] is False
    assert context["full_dashboard_available"] is True
    assert request(client, 1).status_code == 200
    assert (
        request(
            client, 1, "/api/indicators/query", method="POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 200
    )
    assert (
        request(
            client, 1, "/api/deposits", method="POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 503
    )
    assert (
        request(
            client, 1, "/api/indicators/new-write", method="POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 503
    )
    company["subscription"]["overrides"] = {"analytics.overview": {"mode": "deny"}}
    assert request(client, 1).status_code == 403
    assert request(client, 2).status_code == 503


def test_recovery_rate_limit_is_per_transport_peer(gateway):
    client, repo, registry, calls = gateway
    app = client.app
    path = "https://api.client1.example.org/api/auth/recovery/telegram"
    origin = {"origin": "https://client1.example.org"}
    with TestClient(app, client=("192.0.2.11", 1000)) as first:
        for _ in range(10):
            assert first.get(path, headers=origin).status_code == 200
        assert (
            first.get(path, headers={**origin, "x-forwarded-for": "192.0.2.12"}).status_code == 429
        )
    with TestClient(app, client=("192.0.2.12", 1000)) as second:
        assert second.get(path, headers=origin).status_code == 200


def test_uds_edge_identity_scopes_gateway_recovery_limits(gateway, tmp_path):
    from app.saas_admin.edge_peer import EdgePeer

    client, repo, registry, calls = gateway
    secret = tmp_path / "edge.key"
    secret.write_text("private-edge-" * 4)
    secret.chmod(0o600)
    client.app.state.full_proxy.edge_peer = EdgePeer(secret)
    url = "https://api.client1.example.org/api/auth/recovery/telegram"
    headers = {
        "origin": "https://client1.example.org",
        "x-restcontrol-proxy-token": secret.read_text(),
        "x-restcontrol-client-ip": "192.0.2.30",
    }
    with TestClient(client.app, client=None) as edge:
        assert (
            edge.get(url, headers={**headers, "x-restcontrol-proxy-token": "spoof"}).status_code
            == 403
        )
        for _ in range(10):
            assert edge.get(url, headers=headers).status_code == 200
        assert edge.get(url, headers=headers).status_code == 429
        assert (
            edge.get(url, headers={**headers, "x-restcontrol-client-ip": "192.0.2.31"}).status_code
            == 200
        )


def test_setup_owner_exact_routes_csrf_and_upgrade(gateway):
    client, repo, registry, calls = gateway
    registry.ready = False
    repo.companies["client1.example.org"]["subscription"]["plan_id"] = "full"
    registry.resolve_setup = lambda company: ReadyRuntime(
        company["id"], company["version"], f"/tmp/c_{UUID(company['id']).hex}/portal.sock"
    )
    context = request(client, 1, "/api/saas-context").json()
    assert context["setup_available"] is True
    assert context["full_dashboard_ready"] is False
    assert context["full_dashboard_available"] is False
    assert request(client, 1, "/api/me").status_code == 200
    assert request(client, 1, "/api/management/venues").status_code == 200
    path = f"/api/payment-settings/venues/{UUID(int=5)}/terminals/{UUID(int=6)}"
    assert request(client, 1, path, "POST", json={}).status_code == 403
    assert request(client, 1, path, "POST", headers={"x-csrf-token": "csrf"}).status_code == 200
    assert (
        request(client, 1, path + "/validate", "POST", headers={"x-csrf-token": "csrf"}).status_code
        == 200
    )
    assert request(client, 1, "/api/overview").status_code == 403
    assert (
        request(
            client, 1, "/api/management/accounts", "POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 403
    )
    assert (
        request(client, 1, f"/api/guest-deposits/{UUID(int=5)}/prepare", "POST").status_code == 503
    )
    assert (
        request(client, 1, "/api/me", headers={"origin": "https://other.example.org"}).status_code
        == 403
    )
    assert request(client, 3, "/api/me").status_code == 403
    original = repo.tenant_actor_session
    repo.tenant_actor_session = lambda token, company_id: (
        ActorContext(UUID(company_id), UUID(int=55), "company_member", "Member"),
        {"csrf_token": "csrf"},
    )
    assert request(client, 1, "/api/management/venues").status_code == 403
    repo.tenant_actor_session = original
    registry.ready = True
    assert request(client, 1, "/api/overview").status_code == 200


def test_partial_runtime_exact_capabilities_and_private_worker_denial(gateway):
    client, repo, registry, calls = gateway
    company = repo.companies["client1.example.org"]
    company["modules"].update(documents=True, deposits=True)
    company["subscription"]["plan_id"] = "full"
    runtime = registry.resolve(company)
    registry.resolve = lambda company: None  # Strict full readiness remains false.
    registry.resolve_working = lambda company: runtime
    registry.features = {
        "analytics.overview": {"read": True, "write": False},
        "documents.waybills": {"read": True, "write": True},
        "documents.dispatch": {"read": True, "write": False},
    }
    assert request(client, 1).status_code == 200
    assert (
        request(
            client, 1, "/api/documents/waybill", "POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 200
    )
    assert (
        request(
            client, 1, "/api/documents/waybill/1/send", "POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 403
    )
    assert request(client, 1, "/api/assistant/status").status_code == 403
    assert (
        request(client, 1, f"/api/guest-deposits/{UUID(int=5)}/prepare", "POST").status_code == 403
    )
    assert (
        request(client, 1, f"/api/guest-deposits/{UUID(int=5)}/reconcile", "POST").status_code
        == 200
    )
    assert request(client, 1, "/api/saas-context").json()["full_dashboard_ready"] is False
    registry.features = {}
    assert request(client, 1).status_code == 403
    # Exact owner configuration survives an unavailable module; no general writes.
    assert request(client, 1, "/api/management/venues").status_code == 200
    assert (
        request(
            client, 1, "/api/management/accounts", "POST", headers={"x-csrf-token": "csrf"}
        ).status_code
        == 403
    )
    grant = VerifierGrant(company["id"], "documents-worker", "s" * 40)
    with TestClient(create_verifier_app(repo, [grant], runtime_registry=registry)) as verifier:
        assert (
            verifier.post(
                f"/verify/{company['id']}/feature",
                headers={"authorization": "Bearer " + grant.secret},
                json={"feature": "documents.dispatch"},
            ).status_code
            == 403
        )
    with TestClient(create_verifier_app(repo, [grant])) as verifier:
        assert (
            verifier.post(
                f"/verify/{company['id']}/feature",
                headers={"authorization": "Bearer " + grant.secret},
                json={"feature": "documents.dispatch"},
            ).status_code
            == 503
        )


def test_metadata_projection_and_payment_revocation_before_upstream(gateway):
    client, repo, registry, calls = gateway
    company = repo.companies["client1.example.org"]
    company["modules"]["deposits"] = True
    company["subscription"]["plan_id"] = "full"
    registry.features = {feature: {"read": True, "write": True} for feature in FEATURES}
    registry.features["assistant.chat"] = {"read": False, "write": False}
    registry.metadata = {
        "sections": ["overview", "sales", "finance", "unknown"],
        "user": {"id": "own", "role": "member", "sections": ["sales"], "warehouses": ["own-store"]},
        "can_manage": False,
    }
    metadata = request(client, 1, "/api/me").json()
    assert metadata["sections"] == ["overview", "sales"]
    assert metadata["user"] == registry.metadata["user"]
    assert metadata["can_manage"] is False
    path = f"/api/payment-settings/venues/{UUID(int=5)}/terminals/{UUID(int=6)}"
    assert request(client, 1, path, "POST", headers={"x-csrf-token": "csrf"}).status_code == 200
    assert registry.invalidated == company["id"]
    assert (
        request(client, 1, f"/api/guest-deposits/{UUID(int=5)}/prepare", "POST").status_code == 403
    )
    assert (
        request(client, 1, f"/api/guest-deposits/{UUID(int=5)}/reconcile", "POST").status_code
        == 200
    )


def test_private_account_creation_requires_own_write_evidence():
    from types import SimpleNamespace

    company = str(UUID(int=1))
    grant = VerifierGrant(company, "portal", "p" * 40)
    calls = []
    accounts = SimpleNamespace(create=lambda *args: calls.append(args) or {"id": "created"})
    registry = Registry()
    registry.features = {"management.users": {"read": True, "write": False}}
    headers = {"authorization": "Bearer " + grant.secret}
    path = f"/verify/{company}/account-create"
    with TestClient(
        create_verifier_app(Repo(), [grant], company_accounts=accounts, runtime_registry=registry)
    ) as client:
        assert client.post(path, headers=headers, json={}).status_code == 403
        registry.features["management.users"]["write"] = True
        assert client.post(path, headers=headers, json={}).status_code == 200
    assert len(calls) == 1
    with TestClient(create_verifier_app(Repo(), [grant], company_accounts=accounts)) as client:
        assert client.post(path, headers=headers, json={}).status_code == 503
    assert len(calls) == 1


def test_guest_shortlink_domain_has_public_only_proxy_and_no_cookie(gateway):
    client, repo, registry, calls = gateway
    company = repo.companies["client1.example.org"]
    repo.company_for_payment_domain = lambda host: (
        {**company, "domain": "client1.example.org"} if host == "pay.customer.net" else None
    )
    code = "A" * 32
    headers = {
        "Origin": "https://pay.customer.net",
        "Sec-Fetch-Site": "cross-site",
        "Cookie": "saas_tenant_session=private",
    }
    response = client.get("https://api.pay.customer.net/api/guest-links/" + code, headers=headers)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://pay.customer.net"
    assert "access-control-allow-credentials" not in response.headers
    assert calls[-1].headers["host"] == "api.client1.example.org"
    assert calls[-1].headers["cookie"] == "saas_tenant_session="
    preflight = client.options(
        "https://api.client1.example.org/api/guest-links/" + code + "/reconcile",
        headers={
            **headers,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 204
    denied = client.get("https://api.pay.customer.net/api/me", headers=headers)
    assert denied.status_code == 403
    denied = client.options(
        "https://api.client1.example.org/api/saas-tenant/client1/auth/login",
        headers={**headers, "Access-Control-Request-Method": "POST"},
    )
    assert denied.status_code == 403


def test_same_origin_gateway_routes_two_companies_without_origin_header(gateway):
    client, repo, registry, calls = gateway
    for index in (1, 2):
        reply = client.get(
            f"https://client{index}.example.org/api/overview",
            headers={
                "Cookie": f"saas_tenant_session={UUID(int=index)}",
                "X-Forwarded-Host": "api.foreign.example.org",
            },
        )
        assert reply.status_code == 200
        assert reply.json()["company"] == str(UUID(int=index))
        assert calls[-1].headers["host"] == f"api.client{index}.example.org"
        assert "access-control-allow-origin" not in reply.headers
    assert (
        client.get(
            "https://client2.example.org/api/overview",
            headers={
                "Cookie": f"saas_tenant_session={UUID(int=1)}",
            },
        ).status_code
        == 401
    )


def test_same_origin_payment_domain_only_public_routes(gateway):
    client, repo, registry, calls = gateway
    company = repo.companies["client1.example.org"]
    repo.company_for_payment_domain = lambda host: (
        {**company, "domain": "client1.example.org"} if host == "pay.customer.net" else None
    )
    context = client.get("https://pay.customer.net/api/saas-context")
    assert context.status_code == 200
    assert context.json()["api_origin"] == "https://pay.customer.net"
    response = client.get(
        "https://pay.customer.net/api/guest-links/" + "A" * 32,
        headers={"Cookie": "saas_tenant_session=private"},
    )
    assert response.status_code == 200
    assert calls[-1].headers["host"] == "api.client1.example.org"
    assert calls[-1].headers["cookie"] == "saas_tenant_session="
    for path in ("/api/me", "/api/saas-tenant/client1/auth/me", "/api/saas-admin/auth/me"):
        assert client.get("https://pay.customer.net" + path).status_code == 403


def test_same_origin_provider_callback_needs_no_browser_origin(gateway):
    client, repo, registry, calls = gateway
    response = client.post(
        f"https://client1.example.org/api/payment-callbacks/{UUID(int=3)}?token=opaque",
        json={},
    )
    assert response.status_code == 200
    assert calls[-1].headers["host"] == "api.client1.example.org"
    assert calls[-1].headers["cookie"] == "saas_tenant_session="
