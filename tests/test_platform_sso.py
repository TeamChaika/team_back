"""Delegation never creates a local owner; proofs and parent revocation are enforced."""

import secrets
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from test_saas_supabase_auth import pg_repo as auth_pg_repo

from app.saas_admin.platform_sso import pkce_challenge
from app.saas_admin.repository import Problem

pg_repo = auth_pg_repo


@pytest.fixture
def sso(pg_repo):
    repo = pg_repo
    with repo.connect(True) as db:
        company = db.execute("SELECT body FROM companies").fetchone()["body"]
        company.update(domain="tenant.example.org", status="active")
        db.execute("UPDATE companies SET body=%s,status=%s", (Jsonb(company), "active"))
    parent, _ = repo.login("existing@example.com", "fake", "peer")
    verifier, state, nonce = [secrets.token_urlsafe(32) for _ in range(3)]
    grant = repo.authorize_platform(parent, repo.company_id, state, nonce, pkce_challenge(verifier))
    return repo, parent, verifier, state, nonce, grant


def exchange(sso, **kwargs):
    repo, parent, verifier, state, nonce, grant = sso
    arguments = dict(
        code=grant["code"],
        verifier=verifier,
        state=state,
        nonce=nonce,
        slug="tenant",
        frontend_origin="https://tenant.example.org",
        api_origin="https://api.tenant.example.org",
    )
    arguments.update(kwargs)
    return repo.exchange_platform(**arguments)


def test_global_actor_without_membership_and_replay(sso):
    repo = sso[0]
    with repo.connect() as db:
        before = db.execute("SELECT count(*) n FROM memberships").fetchone()["n"]
    token, session = exchange(sso)
    assert session["actor"]["kind"] == "platform_owner"
    assert session["user"]["id"] == repo.auth.user_id
    actor = repo.tenant_actor(token, repo.company_id)
    assert actor.auth_user_id == UUID(repo.auth.user_id)
    assert actor.membership_id is None
    assert repo.tenant_session(token, "tenant")["actor"]["kind"] == "platform_owner"
    with repo.connect() as db:
        assert db.execute("SELECT count(*) n FROM memberships").fetchone()["n"] == before
        assert (
            str(db.execute("SELECT actor_id FROM platform_tenant_events").fetchone()["actor_id"])
            == repo.auth.user_id
        )
    with pytest.raises(Problem):
        exchange(sso)


@pytest.mark.parametrize(
    "overrides",
    [
        dict(slug="other"),
        dict(frontend_origin="https://evil.example"),
        dict(api_origin="https://api.other.example"),
        dict(state="x" * 43),
        dict(nonce="x" * 43),
        dict(verifier="x" * 43),
    ],
)
def test_wrong_scope_and_proofs(sso, overrides):
    with pytest.raises(Problem):
        exchange(sso, **overrides)
    assert exchange(sso)[1]["actor"]["kind"] == "platform_owner"


def test_parent_logout_revokes_child(sso):
    repo, parent = sso[:2]
    token, _ = exchange(sso)
    repo.logout(parent)
    with pytest.raises(Problem):
        repo.tenant_session(token, "tenant")
    with repo.connect() as db:
        assert db.execute("SELECT count(*) n FROM platform_tenant_sessions").fetchone()["n"] == 0


def test_owner_disable_revokes_child(sso):
    repo = sso[0]
    token, _ = exchange(sso)
    with repo.connect(True) as db:
        db.execute("UPDATE platform_memberships SET active=false")
    with pytest.raises(Problem):
        repo.tenant_session(token, "tenant")


def test_grant_expiry(sso):
    with sso[0].connect(True) as db:
        db.execute("UPDATE platform_sso_codes SET expires=0")
    with pytest.raises(Problem):
        exchange(sso)


def test_domain_reassignment(sso):
    repo = sso[0]
    token, _ = exchange(sso)
    with repo.connect(True) as db:
        body = db.execute("SELECT body FROM companies").fetchone()["body"]
        body["domain"] = "new.example.org"
        db.execute("UPDATE companies SET body=%s", (Jsonb(body),))
    with pytest.raises(Problem):
        repo.tenant_session(token, "tenant")


def test_parent_refresh_single_storage(sso):
    import json

    repo = sso[0]
    token, _ = exchange(sso)
    with repo.connect(True) as db:
        row = db.execute("SELECT tokens_ciphertext FROM sessions").fetchone()
        pair = json.loads(repo.vault.decrypt(row["tokens_ciphertext"]))
        pair["expires_at"] = 0
        db.execute(
            "UPDATE sessions SET tokens_ciphertext=%s", (repo.vault.encrypt(json.dumps(pair)),)
        )
    assert repo.tenant_session(token, "tenant")["actor"]["kind"] == "platform_owner"
    assert repo.auth.refresh_calls == 1
    with repo.connect() as db:
        cols = db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='restcontrol' AND table_name='platform_tenant_sessions'"
        ).fetchall()
    assert not {"tokens_ciphertext", "refresh_token"} & {r["column_name"] for r in cols}


def test_owner_enters_two_companies_without_new_memberships(sso):
    from uuid import uuid4

    repo, parent, verifier, state, nonce, _ = sso
    first, _ = exchange(sso)
    second_id = str(uuid4())
    with repo.connect(True) as db:
        body = db.execute("SELECT body FROM companies").fetchone()["body"]
        body.update(id=second_id, slug="second", domain="second.example.org", name="Second")
        db.execute(
            "INSERT INTO companies VALUES (%s,%s,%s,NULL,1,%s)",
            (second_id, "second", "active", Jsonb(body)),
        )
        before = db.execute("SELECT count(*) n FROM memberships").fetchone()["n"]
    grant = repo.authorize_platform(parent, second_id, state, nonce, pkce_challenge(verifier))
    second, _ = repo.exchange_platform(
        grant["code"],
        verifier,
        state,
        nonce,
        "second",
        "https://second.example.org",
        "https://api.second.example.org",
    )
    a, b = repo.tenant_actor(first, repo.company_id), repo.tenant_actor(second, second_id)
    assert a.auth_user_id == b.auth_user_id and a.company_id != b.company_id
    with pytest.raises(Problem):
        repo.tenant_session(first, "second")
    with pytest.raises(Problem):
        repo.tenant_session(second, "tenant")
    with repo.connect() as db:
        assert db.execute("SELECT count(*) n FROM memberships").fetchone()["n"] == before


def test_concurrent_exchange_consumes_once(sso):
    from concurrent.futures import ThreadPoolExecutor

    def consume(_):
        try:
            exchange(sso)
            return True
        except Problem:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]


def test_tenant_portal_uses_verified_actor_and_csrf(sso):
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.web.auth import tenant_actor_from_request

    repo = sso[0]
    token, session = exchange(sso)
    runtime = SimpleNamespace(mode="tenant", company_id=UUID(repo.company_id))
    app = SimpleNamespace(state=SimpleNamespace(saas_auth_repository=repo))

    def request(method, csrf=None):
        headers = [(b"cookie", f"saas_tenant_session={token}".encode())]
        if csrf:
            headers.append((b"x-csrf-token", csrf.encode()))
        return Request(
            {
                "type": "http",
                "method": method,
                "headers": headers,
                "app": app,
                "path": "/api/documents",
            }
        )

    actor = asyncio.run(tenant_actor_from_request(request("GET"), runtime))
    assert actor.kind == "platform_owner" and actor.company_id == runtime.company_id
    with pytest.raises(HTTPException):
        asyncio.run(tenant_actor_from_request(request("POST"), runtime))
    assert (
        asyncio.run(tenant_actor_from_request(request("POST", session["csrf_token"]), runtime))
        == actor
    )
    from uuid import uuid4

    other_id = str(uuid4())
    with repo.connect(True) as db:
        body = db.execute("SELECT body FROM companies").fetchone()["body"]
        body.update(id=other_id, slug="wrong", domain="wrong.example.org")
        db.execute(
            "INSERT INTO companies VALUES (%s,%s,%s,NULL,1,%s)",
            (other_id, "wrong", "active", Jsonb(body)),
        )
    wrong = SimpleNamespace(mode="tenant", company_id=UUID(other_id))
    with pytest.raises(HTTPException):
        asyncio.run(tenant_actor_from_request(request("GET"), wrong))


def test_full_owner_scope_never_reads_or_creates_local_profile(sso):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.web.repository import Repository

    repo = sso[0]
    token, _ = exchange(sso)
    actor = repo.tenant_actor(token, repo.company_id)
    department, store = uuid4(), uuid4()
    with repo.connect(True) as db:
        db.execute("CREATE SCHEMA chaika")
        db.execute(
            "CREATE TABLE chaika.corporate_nodes(id uuid,parent_id uuid,name text,"
            "code text,type text,source_id text)"
        )
        db.execute("CREATE TABLE chaika.stores(id uuid,parent_id uuid,name text,source_id text)")
        db.execute(
            "CREATE TABLE chaika.rms_bindings(source_id text,department_id uuid,"
            "state text,chain_source_id text)"
        )
        db.execute(
            "INSERT INTO chaika.corporate_nodes VALUES "
            "(%s,NULL,'Restaurant','R','DEPARTMENT','primary')",
            (department,),
        )
        db.execute(
            "INSERT INTO chaika.stores VALUES (%s,%s,'Store','primary')", (store, department)
        )
    portal = Repository.__new__(Repository)
    portal.runtime = SimpleNamespace(mode="tenant", company_id=actor.company_id)
    portal.connection = repo.connect
    scope = portal.actor_scope(actor)
    assert scope.user["is_portal_admin"] and scope.user["role"] == "owner"
    assert scope.user["id"] == actor.auth_user_id and scope.actor == actor
    assert store in scope.store_ids and department in scope.ids
    assert {"deposits", "employees", "outgoing", "writeoffs", "indicators"} <= set(
        scope.user["sections"]
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        portal.actor_scope(actor, uuid4())
    with repo.connect() as db:
        assert db.execute("SELECT to_regclass('chaika.web_users') t").fetchone()["t"] is None


def test_same_origin_exchange_preserves_delegation_and_replay_protection(sso):
    token, session = exchange(sso, api_origin="https://tenant.example.org")
    assert session["actor"]["kind"] == "platform_owner"
    assert sso[0].tenant_session(token, "tenant")["actor"]["kind"] == "platform_owner"
    with pytest.raises(Problem):
        exchange(sso, api_origin="https://tenant.example.org")
