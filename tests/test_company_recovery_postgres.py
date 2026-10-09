"""Recovery has a real company DB proof and a synthetic Auth provider."""

import hashlib
import secrets
from uuid import uuid4

import psycopg
import pytest

from app.saas_admin.repository import Problem
from app.tenancy.sql import render
from tests.test_company_accounts_postgres import accounts as accounts
from tests.test_company_accounts_postgres import body, create
from tests.test_tenant_migrations_postgres import empty_database as empty_database


def proof(accounts):
    service, repo, auth, db, _, runtimes, _, _ = accounts
    payload = body()
    user = create(accounts, payload)["id"]
    runtime = runtimes[0]
    db.execute(render("UPDATE {documents}.authentication_user SET telegram_id=123", runtime))
    raw = secrets.token_urlsafe(32)
    db.execute(
        render(
            "INSERT INTO {documents}.native_password_recovery "
            "(token_hash,portal_id,user_id,telegram_id,revision,expires_at) "
            "SELECT %s,supabase_id,user_id,123,revision,now()+interval '10 minutes' "
            "FROM {documents}.portal_documents_userlink",
            runtime,
        ),
        (hashlib.sha256(raw.encode()).hexdigest(),),
    )
    token, _ = repo.tenant_login("company0", payload["email"], payload["password"], "recovery-test")
    return raw, user, token


def test_recovery_consumes_real_proof_revokes_sessions_and_preserves_grants(accounts, monkeypatch):
    service, repo, auth, db, _, runtimes, _, _ = accounts
    raw, user, token = proof(accounts)
    calls = []

    def reset(subject, password):
        calls.append((str(subject), password))
        assert db.execute("SELECT count(*) FROM restcontrol.tenant_sessions").fetchone()[0] == 0
        assert db.execute(
            render(
                "SELECT claimed_at IS NOT NULL FROM {documents}.native_password_recovery",
                runtimes[0],
            )
        ).fetchone()[0]
        return {"id": str(subject)}

    monkeypatch.setattr(auth, "reset_password", reset, raising=False)
    assert service.recover_password(str(runtimes[0].company_id), raw, "new-password") == {
        "ok": True,
        "completed": True,
    }
    assert calls == [(user, "new-password")]
    with pytest.raises(Problem):
        repo.tenant_actor_session(token, str(runtimes[0].company_id))
    with pytest.raises(Problem):
        service.recover_password(str(runtimes[0].company_id), raw, "replay-password")
    assert len(calls) == 1
    assert db.execute(
        render(
            "SELECT sections,is_portal_admin,password_change_required FROM {analytics}.web_users",
            runtimes[0],
        )
    ).fetchone() == ([], False, False)


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign",
        "expired",
        "revision",
        "inactive-profile",
        "inactive-member",
        "shared",
        "platform",
        "ambiguous",
        "provider-mismatch",
    ],
)
def test_recovery_fails_closed_and_never_replays_provider(accounts, monkeypatch, mutation):
    service, _, auth, db, _, runtimes, _, _ = accounts
    raw, user, _ = proof(accounts)
    runtime = runtimes[0]
    calls = []

    def reset(subject, password):
        calls.append(str(subject))
        if mutation == "ambiguous":
            raise Problem(503, "auth_ambiguous", "Synthetic lost response")
        return {"id": str(uuid4())}

    monkeypatch.setattr(auth, "reset_password", reset, raising=False)
    company = str(runtime.company_id)
    if mutation == "foreign":
        company = str(runtimes[1].company_id)
    elif mutation == "expired":
        db.execute(
            render(
                "UPDATE {documents}.native_password_recovery "
                "SET expires_at=now()-interval '1 second'",
                runtime,
            )
        )
    elif mutation == "revision":
        db.execute(
            render("UPDATE {documents}.portal_documents_userlink SET revision=revision+1", runtime)
        )
    elif mutation == "inactive-profile":
        db.execute(render("UPDATE {analytics}.web_users SET active=false", runtime))
    elif mutation == "inactive-member":
        db.execute("UPDATE restcontrol.memberships SET active=false")
    elif mutation == "shared":
        db.execute("UPDATE restcontrol.memberships SET auth_exclusive=false")
    elif mutation == "platform":
        db.execute(
            "INSERT INTO restcontrol.platform_memberships "
            "VALUES(%s,%s,'another@example.com','Owner',true)",
            (uuid4(), user),
        )
    with pytest.raises(Problem):
        service.recover_password(company, raw, "new-password")
    with pytest.raises(Problem):
        service.recover_password(company, raw, "replay-password")
    assert len(calls) == (1 if mutation in {"ambiguous", "provider-mismatch"} else 0)


def test_identity_recovery_role_still_has_no_document_table_access(accounts):
    service, _, _, _, _, runtimes, _, _ = accounts
    target = service.targets[str(runtimes[0].company_id)]
    with target.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(render("SELECT * FROM {documents}.native_password_recovery", runtimes[0]))


def test_recovery_holds_binding_profile_and_membership_through_provider(accounts, monkeypatch):
    service, _, auth, operator, dsn, runtimes, _, _ = accounts
    raw, _, _ = proof(accounts)

    def reset(subject, password):
        with psycopg.connect(dsn, autocommit=True) as competing:
            competing.execute("SET lock_timeout='80ms'")
            for statement in (
                render(
                    "UPDATE {documents}.portal_documents_userlink SET revision=revision+1",
                    runtimes[0],
                ),
                render("UPDATE {analytics}.web_users SET active=false", runtimes[0]),
                "UPDATE restcontrol.memberships SET active=false",
            ):
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    competing.execute(statement)
        return {"id": str(subject)}

    monkeypatch.setattr(auth, "reset_password", reset, raising=False)
    assert service.recover_password(str(runtimes[0].company_id), raw, "new-password")["completed"]
