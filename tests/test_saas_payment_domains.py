"""Real registry: guest domain approval never grants dashboard or auth access."""

import psycopg
import pytest

from app.saas_admin.payment_domains import PaymentDomainActivation, PaymentDomains
from app.saas_admin.repository import Problem
from tests.test_saas_admin_postgres import database as database
from tests.test_saas_admin_postgres import imported as imported
from tests.test_saas_admin_postgres import pg_server as pg_server
from tests.test_tenant_integrations import tenant as tenant

PATH = "/api/saas-tenant/company-one/payment-domain"


def request_domain(client, domain="pay.customer.net"):
    current = client.get(PATH).json()
    return client.post(
        PATH,
        json={
            "domain": domain,
            "expected_version": current["company_version"],
            "expected_revision": current["revision"],
        },
    )


def activate(repo, first, current):
    with repo.connect() as db:
        actor = db.execute("SELECT * FROM platform_memberships").fetchone()
    return PaymentDomains(repo).activate(
        first["id"],
        actor,
        PaymentDomainActivation(
            expected_version=first["version"],
            expected_revision=current["revision"],
            domain=current["domain"],
            dns_verified=True,
            tls_verified=True,
            frontend_verified=True,
        ),
    )


def test_guest_request_activation_cors_and_no_auth(tenant):
    repo, client, first, _, _ = tenant
    with repo.connect(True) as db:
        configured = repo._get(db, first["id"])
        configured["timezone"] = "Asia/Vladivostok"
        repo._write_company(db, configured)
    reply = request_domain(client)
    assert reply.status_code == 200, reply.text
    pending = reply.json()
    assert pending["status"] == "pending"
    assert pending["company_version"] == first["version"]
    assert not repo.company_for_payment_domain("pay.customer.net")
    assert repo.payment_origin(first["id"]) == ""
    active = activate(repo, first, pending)
    assert active["payment_origin"] == "https://pay.customer.net"
    assert repo.get(first["id"])["version"] == first["version"]
    assert repo.company_for_domain("pay.customer.net") is None
    context = client.get(
        "/api/saas-context",
        headers={"Origin": "https://pay.customer.net", "Sec-Fetch-Site": "cross-site"},
    )
    assert context.status_code == 200, context.text
    assert context.json()["surface"] == "payment"
    assert context.json()["company"]["timezone"] == "Asia/Vladivostok"
    assert context.json()["api_origin"] == "https://api.one.example.com"
    assert "access-control-allow-credentials" not in context.headers
    alias = client.get(
        "https://api.pay.customer.net/api/saas-context",
        headers={"Origin": "https://pay.customer.net"},
    )
    assert alias.status_code == 200
    assert alias.json()["api_origin"] == "https://api.one.example.com"
    for path in (
        "/api/me",
        "/api/saas-tenant/company-one/auth/me",
        "/api/saas-tenant/company-one/integrations",
        "/api/auth/recovery/telegram",
    ):
        denied = client.get(path, headers={"Origin": "https://pay.customer.net"})
        assert denied.status_code == 403, (path, denied.text)
        assert denied.json()["detail"]["code"] == "guest_boundary"
    assert (
        client.get("/api/saas-context", headers={"Origin": "https://evil.net"}).status_code == 403
    )
    assert (
        client.get(
            "https://api.two.example.com/api/saas-context",
            headers={"Origin": "https://pay.customer.net"},
        ).status_code
        == 403
    )


def test_unique_domains_platform_collision_csrf_and_revocation(tenant):
    repo, client, first, second, _ = tenant
    for host in (
        "one.example.com",
        "api.one.example.com",
        "two.example.com",
        "api.two.example.com",
        "rc.chaika.team",
        "api.rc.chaika.team",
    ):
        assert request_domain(client, host).status_code == 409
    assert (
        client.post(
            PATH,
            json={
                "domain": "pay.customer.net",
                "expected_version": first["version"],
                "expected_revision": 1,
            },
            headers={"X-CSRF-Token": "bad"},
        ).status_code
        == 403
    )
    pending = request_domain(client).json()
    activate(repo, first, pending)
    with pytest.raises(psycopg.errors.UniqueViolation), repo.connect(True) as db:
        db.execute(
            "UPDATE companies SET domain='api.pay.customer.net' WHERE id=%s", (second["id"],)
        )
    with pytest.raises(psycopg.errors.UniqueViolation), repo.connect(True) as db:
        db.execute(
            "INSERT INTO company_payment_domains(company_id,domain,status) "
            "VALUES(%s,'api.pay.customer.net','pending')",
            (second["id"],),
        )
    changed = request_domain(client, "newpay.customer.net")
    assert changed.status_code == 200
    assert changed.json()["status"] == "pending"
    assert not repo.company_for_payment_domain("pay.customer.net")
    assert repo.payment_origin(first["id"]) == ""
    assert repo.get(first["id"])["version"] == first["version"]


def test_owner_only_activation_requires_all_checks_and_expected_revision(tenant):
    repo, client, first, _, _ = tenant
    current = request_domain(client).json()
    service = PaymentDomains(repo)
    with repo.connect() as db:
        actor = db.execute("SELECT * FROM platform_memberships").fetchone()
    body = PaymentDomainActivation(
        expected_version=first["version"],
        expected_revision=current["revision"],
        domain=current["domain"],
        dns_verified=True,
        tls_verified=False,
        frontend_verified=True,
    )
    with pytest.raises(Problem) as invalid:
        service.activate(first["id"], actor, body)
    assert invalid.value.code == "domain_not_verified"
    with pytest.raises(Problem) as denied:
        service.activate(first["id"], {"id": "00000000-0000-0000-0000-000000000000"}, body)
    assert denied.value.status == 403
    assert not repo.company_for_payment_domain("pay.customer.net")


def test_unchanged_domain_save_is_idempotent_and_changes_are_audited(tenant):
    repo, client, first, _, _ = tenant
    pending = request_domain(client).json()
    active = activate(repo, first, pending)
    before = client.get(PATH).json()
    repeated = request_domain(client)
    assert repeated.status_code == 200
    assert repeated.json() == before
    assert repeated.json()["status"] == "active"
    assert repeated.json()["revision"] == active["revision"]
    removed = request_domain(client, None)
    assert removed.status_code == 200
    assert removed.json()["status"] == "unconfigured"
    with repo.connect() as db:
        actions = [
            row["action"]
            for row in db.execute(
                "SELECT action FROM tenant_events WHERE company_id=%s", (first["id"],)
            )
        ]
    assert actions.count("payment_domain_requested") == 1
    assert actions.count("payment_domain_removed") == 1
