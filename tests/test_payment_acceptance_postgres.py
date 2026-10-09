"""Real disposable central/tenant PostgreSQL; all provider and health HTTP is fake."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from app.saas_admin.repository import digest
from app.saas_admin.runtime_registry import SETUP_CHECKS, RuntimeRegistry
from app.tenant_payments.acceptance import OwnerAcceptanceAuthority, OwnerPaymentAcceptance
from app.tenant_payments.acceptance_cli import parser
from app.tenant_payments.models import TerminalInput
from app.tenant_payments.service import TenantPayments
from app.tenant_payments.store import PaymentStore, payment_role
from tests.test_company_accounts_postgres import accounts as accounts
from tests.test_tenant_migrations_postgres import empty_database  # noqa: F401
from tests.test_tenant_payments_postgres import Provider, TestVault, setup


@pytest.fixture
def acceptance(accounts, tmp_path, monkeypatch):
    _, repo, _, admin, dsn, runtimes, tokens, parent = accounts
    admin.execute(
        Path("supabase/migrations/20261008235000_restcontrol_runtime_provisioning.sql").read_text()
    )
    built = []
    for runtime, (token, _) in zip(runtimes, tokens, strict=True):
        with repo.connect() as db:
            company = repo._get(db, str(runtime.company_id))
        company.update(
            chain_url="",
            rms=[],
            modules={"deposits": True},
            subscription={
                "policy": "plans_v1",
                "plan_id": "full",
                "status": "active",
                "start_date": "2020-01-01",
            },
        )
        with repo.connect(True) as db:
            repo._write_company(db, company)
            db.execute(
                "INSERT INTO runtime_provisioning(company_id,configuration_version,"
                "socket_path,checks) "
                "VALUES(%s,%s,%s,%s)",
                (
                    runtime.company_id,
                    1,
                    str(runtime.runtime_path("portal.sock")),
                    Jsonb({key: {"ok": True, "evidence": "synthetic"} for key in SETUP_CHECKS}),
                ),
            )
        path = tmp_path / (runtime.key + ".acceptance")
        path.write_text(token)
        path.chmod(0o600)

        def current(company_id, version):
            company = repo.get(company_id)
            if company["version"] != version or company["status"] != "active":
                raise ValueError("stale configuration")
            return company

        operator = SimpleNamespace(
            runtime=runtime,
            repo=repo,
            current=current,
            config={"acceptance_session_file": str(path)},
        )
        store = PaymentStore(runtime, make_conninfo(dsn, user=payment_role(runtime)), TestVault())
        provider = Provider()
        service = TenantPayments(
            store, lambda mode, p=provider: p, feature_authorizer=lambda _: False
        )
        authority = OwnerAcceptanceAuthority(operator)
        built.append((OwnerPaymentAcceptance(service, authority), provider, path))

    def client(**kwargs):
        class Health:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, headers):
                company = next(
                    repo.get(str(r.company_id))
                    for r in runtimes
                    if "api." + repo.get(str(r.company_id))["domain"] == headers["host"]
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "company_id": company["id"],
                        "configuration_version": company["version"],
                    },
                )

        return Health()

    monkeypatch.setattr("app.saas_admin.runtime_registry.httpx.Client", client)
    for core, _, _ in built:
        principal, _ = core.authority.verify()
        core.venue, core.terminal = setup(core.store, principal)
    return built, repo, admin, parent, accounts


def intent(core, **changes):
    return core.create_intent(
        **{
            "terminal_id": core.terminal,
            "amount_minor": 50000,
            "mode": "sandbox",
            "request_id": uuid4(),
            "confirmed": True,
            **changes,
        }
    )


def test_acceptance_without_full_readiness_and_repeat_never_posts(acceptance):
    built, repo, _, _, _ = acceptance
    core, provider, _ = built[0]
    assert RuntimeRegistry(repo).resolve(repo.get(str(core.store.runtime.company_id))) is None
    saved = intent(core)
    original_create = provider.create

    async def check_committed(**kwargs):
        with core.store.connection() as db:
            row = core.store.execute(
                db, "SELECT * FROM {payments}.acceptance_intents WHERE id=%s", (saved["id"],)
            ).fetchone()
            assert row["consumed_at"] and row["attempt_id"]
        return await original_create(**kwargs)

    provider.create = check_committed
    result = asyncio.run(core.execute(saved["id"]))
    assert result["attempt_id"] and provider.calls == 1
    assert result["payment"]["payment_url"] == "https://provider.example/pay"
    assert "qr_image" in result["payment"]
    assert "guest_url" not in str(result) and "token=" not in str(result)
    assert "synthetic-key" not in str(result) and "session_hash" not in str(result)
    assert intent(core, request_id=saved["request_id"])["id"] == saved["id"]
    asyncio.run(core.execute(saved["id"]))
    assert provider.calls == 1
    # Ordinary guest creation remains denied by ordinary feature authorization.
    with pytest.raises(HTTPException):
        asyncio.run(core.service.require_creation())
    with core.store.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            core.store.execute(
                db,
                "UPDATE {payments}.acceptance_intents SET amount_minor=100 WHERE id=%s",
                (saved["id"],),
            )


def test_confirmation_mode_terminal_and_company_fail_closed(acceptance):
    built, _, _, _, _ = acceptance
    core, provider, _ = built[0]
    foreign, other, _ = built[1]
    for changes in (
        {"confirmed": False},
        {"mode": "live"},
        {"terminal_id": foreign.terminal},
        {"amount_minor": 0},
        {"amount_minor": 101},
    ):
        with pytest.raises(HTTPException):
            intent(core, **changes)
    saved = intent(core)
    with pytest.raises(HTTPException):
        asyncio.run(foreign.execute(saved["id"]))
    assert provider.calls == other.calls == 0
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "--config",
                "private.json",
                "create",
                "--mode",
                "sandbox",
                "--terminal",
                str(core.terminal),
                "--amount-minor",
                "100",
                "--request-id",
                str(uuid4()),
            ]
        )


def test_unknown_survives_restart_and_only_reconciles_same_attempt(acceptance):
    built, _, _, _, _ = acceptance
    core, provider, _ = built[0]
    provider.error = True
    saved = intent(core)
    first = asyncio.run(core.execute(saved["id"]))
    restarted = OwnerPaymentAcceptance(core.service, core.authority)
    second = asyncio.run(restarted.execute(saved["id"]))
    assert first["attempt_id"] == second["attempt_id"] and provider.calls == 1
    assert core.store.check_context(UUID(first["attempt_id"]))["state"] == "unknown"


def test_stale_configuration_prerequisites_feature_and_expired_parent(acceptance):
    built, repo, _, parent, _ = acceptance
    core, provider, _ = built[0]
    company = repo.get(str(core.store.runtime.company_id))
    saved = intent(core)
    with repo.connect(True) as db:
        db.execute(
            "UPDATE runtime_provisioning SET checks='{}' WHERE company_id=%s", (company["id"],)
        )
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    with repo.connect(True) as db:
        db.execute(
            "UPDATE runtime_provisioning SET checks=%s WHERE company_id=%s",
            (Jsonb({key: {"ok": True, "evidence": "test"} for key in SETUP_CHECKS}), company["id"]),
        )
        company["modules"]["deposits"] = False
        repo._write_company(db, company)
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    with repo.connect(True) as db:
        company["modules"]["deposits"] = True
        company["version"] = 2
        repo._write_company(db, company)
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    with repo.connect(True) as db:
        company["version"] = 1
        repo._write_company(db, company)
        db.execute("UPDATE sessions SET expires=0 WHERE token_hash=%s", (digest(parent),))
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    assert provider.calls == 0


def test_terminal_drift_and_revocation_during_check_prevent_post(acceptance):
    built, repo, _, parent, _ = acceptance
    core, provider, _ = built[0]
    saved = intent(core)
    principal, _ = core.authority.verify()
    core.store.save_terminal(
        principal,
        core.venue,
        core.terminal,
        TerminalInput(name="Rotated", mode="sandbox", revision=1),
    )
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    saved = intent(core)
    check = provider.check_terminal

    async def revoke(**kwargs):
        result = await check(**kwargs)
        with repo.connect(True) as db:
            db.execute("UPDATE sessions SET expires=0 WHERE token_hash=%s", (digest(parent),))
        return result

    provider.check_terminal = revoke
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    assert provider.calls == 0


def test_staff_and_expired_child_are_denied(acceptance):
    from tests.test_company_accounts_postgres import body, create

    built, repo, _, _, fixture = acceptance
    core, provider, path = built[0]
    saved_token = path.read_text()
    payload = body()
    employee = create(fixture, payload)
    # Remove password-change gating to prove employee kind itself denies acceptance.
    with repo.connect(True) as db:
        db.execute(
            "UPDATE memberships SET must_change=false WHERE auth_user_id=%s", (employee["id"],)
        )
    staff_token, _ = repo.tenant_login("company0", payload["email"], payload["password"], "staff")
    path.write_text(staff_token)
    actor, _ = repo.tenant_actor_session(staff_token, str(core.store.runtime.company_id))
    assert actor.kind == "company_member"
    with pytest.raises(HTTPException):
        intent(core)
    path.write_text(saved_token)
    with repo.connect(True) as db:
        db.execute(
            "UPDATE platform_tenant_sessions SET expires=0 WHERE token_hash=%s",
            (digest(saved_token),),
        )
    with pytest.raises(HTTPException):
        intent(core)
    assert provider.calls == 0


def test_replacement_owner_session_cannot_consume_but_can_reconcile(acceptance):
    from app.saas_admin.platform_sso import pkce_challenge

    built, repo, _, parent, _ = acceptance
    core, provider, path = built[0]
    saved = intent(core)
    company = repo.get(str(core.store.runtime.company_id))
    verifier = "r" * 48
    grant = repo.authorize_platform(
        parent, company["id"], "state", "nonce", pkce_challenge(verifier)
    )
    token, _ = repo.exchange_platform(
        grant["code"],
        verifier,
        "state",
        "nonce",
        company["slug"],
        "https://" + company["domain"],
        "https://api." + company["domain"],
    )
    path.write_text(token)
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    with pytest.raises(HTTPException):
        intent(core, request_id=saved["request_id"])
    assert asyncio.run(core.reconcile(saved["id"]))["attempt_id"] is None
    assert provider.calls == 0


def test_concurrent_execution_posts_once(acceptance):
    built, _, _, _, _ = acceptance
    core, provider, _ = built[0]
    saved = intent(core)

    async def run():
        return await asyncio.gather(*(core.execute(saved["id"]) for _ in range(5)))

    results = asyncio.run(run())
    assert len({result["attempt_id"] for result in results}) == 1
    assert provider.calls == 1


def test_paid_acceptance_proof_survives_config_but_not_terminal_change(acceptance):
    from app.saas_admin.provisioning import PendingCheck
    from app.saas_admin.runtime_operator import RuntimeOperator
    from app.tenant_payments.provider import CreatedOperation

    built, repo, _, _, _ = acceptance
    core, provider, _ = built[0]
    saved = intent(core)
    provider.status = "paid"

    async def create_with_qr_evidence(**kwargs):
        provider.calls += 1
        return CreatedOperation(
            provider.operation_id,
            "https://provider.example/pay",
            currency_evidence="sbp_qr_payload",
            qr_payload="https://qr.nspk.ru/AD10102T192IVSCK87ERR4FDK14GNOR7?type=02&bank=100000000241&sum=50000&cur=RUB&crc=09E8",
        )

    provider.create = create_with_qr_evidence
    result = asyncio.run(core.execute(saved["id"]))
    assert result["deposit"]["status"] == "paid"
    operator = object.__new__(RuntimeOperator)
    operator.runtime = core.store.runtime
    operator.config = {"payments_dsn": core.store.dsn}
    operator.company = repo.get(str(core.store.runtime.company_id))
    assert operator.payments()["ok"]
    operator.runtime = replace(operator.runtime, configuration_version=2)
    assert operator.payments()["ok"]
    principal, _ = core.authority.verify()
    core.store.save_terminal(
        principal,
        core.venue,
        core.terminal,
        TerminalInput(name="Rotated after proof", mode="sandbox", revision=1),
    )
    with pytest.raises(PendingCheck):
        operator.payments()


def test_pending_prior_version_reconciles_without_another_post(acceptance):
    built, repo, _, _, _ = acceptance
    core, provider, _ = built[0]
    saved = intent(core)
    result = asyncio.run(core.execute(saved["id"]))
    assert result["deposit"]["status"] == "pending" and provider.calls == 1
    company = repo.get(str(core.store.runtime.company_id))
    company["version"] += 1
    with repo.connect(True) as db:
        repo._write_company(db, company)
    core.store.runtime = replace(core.store.runtime, configuration_version=company["version"])
    core.authority.operator.runtime = core.store.runtime
    provider.status = "paid"
    with core.store.connection() as db:
        core.store.execute(
            db,
            "UPDATE {payments}.attempts SET next_check_at=now() WHERE id=%s",
            (UUID(result["attempt_id"]),),
        )
    recovered = asyncio.run(core.reconcile(saved["id"]))
    assert recovered["deposit"]["status"] == "paid" and provider.calls == 1
    with pytest.raises(HTTPException):
        asyncio.run(core.execute(saved["id"]))
    with pytest.raises(HTTPException):
        asyncio.run(built[1][0].reconcile(saved["id"]))
    assert provider.calls == 1
