"""Central Auth side effects with real tenant roles; provider is synthetic, no network."""

import hashlib
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from app.saas_admin.company_accounts import CompanyAccounts, IdentityTarget
from app.saas_admin.platform_sso import pkce_challenge
from app.saas_admin.postgres_repository import PostgresRepository
from app.saas_admin.repository import Problem
from app.saas_admin.vault import Vault
from app.tenancy.migrations import provision_tenant
from app.tenancy.sql import render
from tests.test_tenant_migrations_postgres import empty_database as empty_database


class SyntheticAuth:
    def __init__(self, operator):
        self.operator, self.users, self.tokens = operator, {}, {}
        self.creates = 0
        self.ambiguous = False
        self.password_calls = 0

    def register(self, email, password, marker=None):
        user = {"id": str(uuid4()), "email": email, "password": password}
        self.users[email] = user
        self.operator.execute(
            "INSERT INTO auth.users VALUES(%s,%s,%s)",
            (user["id"], email, Jsonb({"restcontrol_request_id": marker} if marker else {})),
        )
        return user

    def create_user(self, email, password, marker):
        self.creates += 1
        user = self.register(email, password, marker)
        if self.ambiguous:
            raise Problem(503, "auth_ambiguous", "Unknown synthetic result")
        return user

    def login(self, email, password):
        user = self.users.get(email)
        if not user or user["password"] != password:
            raise Problem(401, "invalid_credentials", "Bad synthetic password")
        token = str(uuid4())
        self.tokens[token] = user
        return {
            "access_token": token,
            "refresh_token": token,
            "user_id": user["id"],
            "expires_at": time.time() + 3600,
        }

    def user(self, token):
        return {k: self.tokens[token][k] for k in ("id", "email")}

    def refresh(self, token):
        user = self.tokens[token]
        return self.login(user["email"], user["password"])

    def password(self, token, new):
        self.password_calls += 1
        self.tokens[token]["password"] = new
        return self.user(token)


@pytest.fixture
def accounts(empty_database, tmp_path):
    operator, dsn, tenant = empty_database
    for role in ("anon", "authenticated", "chaika_backend"):
        if not operator.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            operator.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    operator.execute("CREATE SCHEMA auth; CREATE SCHEMA chaika")
    operator.execute(
        "CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,raw_app_meta_data jsonb)"
    )
    operator.execute("CREATE TABLE chaika.web_users(id uuid PRIMARY KEY,role text)")
    for name in (
        "20261008135638_restcontrol_supabase_registry.sql",
        "20261008185718_restcontrol_platform_sso.sql",
        "20261008200905_restcontrol_company_accounts.sql",
    ):
        operator.execute((Path("supabase/migrations") / name).read_text())
    Vault(tmp_path)
    auth = SyntheticAuth(operator)
    user = auth.register("platform@example.com", "owner-password")
    member_id = uuid4()
    operator.execute(
        "INSERT INTO restcontrol.platform_memberships VALUES(%s,%s,%s,%s,true)",
        (member_id, user["id"], user["email"], "Owner"),
    )
    repo = PostgresRepository(
        make_conninfo(dsn, options="-c role=restcontrol_backend"), tmp_path, auth=auth
    )
    targets, runtimes = {}, []
    for i in range(2):
        runtime = tenant()
        provision_tenant(operator, runtime)
        body = {
            "id": str(runtime.company_id),
            "name": f"Company{i}",
            "slug": f"company{i}",
            "domain": f"company{i}.example.com",
            "status": "active",
            "version": 1,
            "archived_at": None,
        }
        with repo.connect(True) as db:
            repo._write_company(db, body)
        targets[str(runtime.company_id)] = IdentityTarget(
            runtime, make_conninfo(dsn, user=f"{runtime.key}_identity_runtime")
        )
        runtimes.append(runtime)
    service = CompanyAccounts(repo, targets)
    parent, _ = repo.login(user["email"], "owner-password", "test")
    tokens = []
    for runtime in runtimes:
        company = next(i for i, r in enumerate(runtimes) if r == runtime)
        verifier = "s" * 48
        code = repo.authorize_platform(
            parent, str(runtime.company_id), "state", "nonce", pkce_challenge(verifier)
        )
        tokens.append(
            repo.exchange_platform(
                code["code"],
                verifier,
                "state",
                "nonce",
                f"company{company}",
                f"https://company{company}.example.com",
                f"https://api.company{company}.example.com",
            )
        )
    return service, repo, auth, operator, dsn, runtimes, tokens, parent


def body(email="employee@example.com"):
    return {
        "request_id": str(uuid4()),
        "email": email,
        "display_name": "Employee",
        "password": "temporary-password",
        "profile_fingerprint": hashlib.sha256(b"profile").hexdigest(),
    }


def create(accounts, payload=None, index=0):
    service, _, _, _, _, runtimes, tokens, _ = accounts
    token, session = tokens[index]
    return service.create(
        str(runtimes[index].company_id), token, session["csrf_token"], payload or body()
    )


def test_create_employee_login_password_and_no_cross_company(accounts):
    service, repo, auth, operator, dsn, runtimes, tokens, _ = accounts
    payload = body()
    value = create(accounts, payload)
    assert create(accounts, payload) == value and auth.creates == 1
    runtime = runtimes[0]
    with repo.connect() as db:
        member = db.execute(
            "SELECT * FROM memberships WHERE auth_user_id=%s", (value["id"],)
        ).fetchone()
        assert (
            member["role"] == "employee"
            and not member["is_primary_admin"]
            and member["must_change"]
        )
    for table in ("web_users", "portal_identity_metadata"):
        assert (
            operator.execute(
                render("SELECT count(*) FROM {analytics}." + table, runtime)
            ).fetchone()[0]
            == 1
        )
        assert (
            operator.execute(
                render("SELECT count(*) FROM {analytics}." + table, runtimes[1])
            ).fetchone()[0]
            == 0
        )
    with psycopg.connect(dsn, user=runtime.database_role, autocommit=True) as db:
        row = db.execute(
            render(
                "SELECT role,is_portal_admin,password_change_required,sections "
                "FROM {analytics}.web_users",
                runtime,
            )
        ).fetchone()
        assert row == ("manager", False, True, [])
        assert (
            db.execute(
                render(
                    "SELECT count(*) FROM {documents}.portal_documents_userlink "
                    "WHERE supabase_id=%s",
                    runtime,
                ),
                (value["id"],),
            ).fetchone()[0]
            == 1
        )
    token, session = repo.tenant_login(
        "company0", payload["email"], payload["password"], "employee"
    )
    actor, verified = repo.tenant_actor_session(token, str(runtime.company_id))
    assert actor.auth_user_id == UUID(value["id"]) and verified["must_change_password"]
    with pytest.raises(Problem):
        repo.tenant_actor_session(token, str(runtimes[1].company_id))
    changed = service.password(
        str(runtime.company_id),
        token,
        session["csrf_token"],
        payload["password"],
        "personal-password",
    )
    assert changed["completed"] and "access_token" not in json.dumps(changed)
    assert (
        repo.tenant_actor_session(changed["token"], str(runtime.company_id))[1][
            "must_change_password"
        ]
        is False
    )
    assert (
        operator.execute(
            render("SELECT password_change_required FROM {analytics}.web_users", runtime)
        ).fetchone()[0]
        is False
    )
    with pytest.raises(Problem):
        create(accounts, {**payload, "password": "different-password"})


def test_unknown_create_reconciles_once_and_existing_foreign_identity_never_attaches(accounts):
    _, _, auth, operator, _, _, _, _ = accounts
    auth.ambiguous = True
    payload = body()
    with pytest.raises(Problem) as ambiguous:
        create(accounts, payload)
    assert ambiguous.value.status == 503
    auth.ambiguous = False
    recovered = create(accounts, payload)
    assert recovered["id"] and auth.creates == 1
    auth.register("foreign@example.com", "foreign-password")
    with pytest.raises(Problem) as duplicate:
        create(accounts, body("foreign@example.com"))
    assert duplicate.value.status == 409 and auth.creates == 1
    assert operator.execute("SELECT count(*) FROM restcontrol.memberships").fetchone()[0] == 1
    assert auth.users["foreign@example.com"]["password"] == "foreign-password"


def test_owner_self_password_keeps_parent_opaque_and_never_creates_personal_profile(accounts):
    service, repo, auth, operator, _, runtimes, tokens, parent = accounts
    token, session = tokens[0]
    changed = service.password(
        str(runtimes[0].company_id),
        token,
        session["csrf_token"],
        "owner-password",
        "new-owner-password",
    )
    assert changed["completed"] and "access_token" not in json.dumps(changed)
    assert repo.session(parent)["user"]["username"] == "platform@example.com"
    assert (
        repo.tenant_actor_session(changed["token"], str(runtimes[0].company_id))[0].kind
        == "platform_owner"
    )
    with pytest.raises(Problem):
        repo.tenant_actor_session(token, str(runtimes[0].company_id))
    assert auth.password_calls == 1
    assert operator.execute("SELECT count(*) FROM restcontrol.memberships").fetchone()[0] == 0
    assert (
        operator.execute(
            render("SELECT count(*) FROM {analytics}.web_users", runtimes[0])
        ).fetchone()[0]
        == 0
    )


def test_verifier_capabilities_csrf_and_current_local_admin_are_enforced(accounts):
    from fastapi.testclient import TestClient

    from app.tenancy.bootstrap import VerifierGrant, create_verifier_app

    service, repo, auth, operator, _, runtimes, tokens, _ = accounts
    company = str(runtimes[0].company_id)
    secret, worker = "p" * 40, "w" * 40
    app = create_verifier_app(
        repo,
        [
            VerifierGrant(company, "portal", secret),
            VerifierGrant(company, "documents-worker", worker),
        ],
        company_accounts=service,
    )
    token, session = tokens[0]
    request_body = {"token": token, "csrf": session["csrf_token"], "account": body()}
    with TestClient(app) as client:
        path = f"/verify/{company}/account-create"
        assert (
            client.post(
                path, headers={"authorization": "Bearer " + worker}, json=request_body
            ).status_code
            == 403
        )
        assert (
            client.post(
                path,
                headers={"authorization": "Bearer " + secret},
                json={**request_body, "csrf": "wrong"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/verify/{runtimes[1].company_id}/account-create",
                headers={"authorization": "Bearer " + secret},
                json=request_body,
            ).status_code
            == 403
        )
        assert auth.creates == 0
        result = client.post(path, headers={"authorization": "Bearer " + secret}, json=request_body)
        assert result.status_code == 200
    employee = result.json()["id"]
    emp_token, emp_session = repo.tenant_login(
        "company0", request_body["account"]["email"], "temporary-password", "peer"
    )
    changed = service.password(
        company, emp_token, emp_session["csrf_token"], "temporary-password", "new-employee-password"
    )
    with pytest.raises(Problem) as denied:
        service.create(company, changed["token"], changed["csrf_token"], body("second@example.com"))
    assert denied.value.status == 403 and auth.creates == 1
    operator.execute(
        render("UPDATE {analytics}.web_users SET is_portal_admin=true WHERE id=%s", runtimes[0]),
        (employee,),
    )
    assert service.create(
        company, changed["token"], changed["csrf_token"], body("second@example.com")
    )["id"]
    operator.execute(
        render("UPDATE {analytics}.web_users SET active=false WHERE id=%s", runtimes[0]),
        (employee,),
    )
    with pytest.raises(Problem) as denied:
        service.create(company, changed["token"], changed["csrf_token"], body("third@example.com"))
    assert denied.value.status == 403 and auth.creates == 2


def test_current_management_and_profile_routes_use_restricted_verifier(accounts, monkeypatch):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from app.tenancy.bootstrap import RestrictedVerifier, VerifierGrant, create_verifier_app
    from app.web import administration
    from app.web.profile import create_profile_router
    from app.web.repository import Scope

    service, repo, _, _, dsn, runtimes, tokens, _ = accounts
    runtime = runtimes[0]
    grant = VerifierGrant(str(runtime.company_id), "portal", "g" * 40)
    private_app = create_verifier_app(repo, [grant], company_accounts=service)
    pool = ConnectionPool(
        make_conninfo(dsn, user=runtime.database_role), kwargs={"row_factory": dict_row}, open=False
    )
    local_repo = SimpleNamespace(runtime=runtime, _pool=pool)
    monkeypatch.setattr(administration, "DB", sql.Identifier(runtime.analytics_schema).as_string())
    payments = SimpleNamespace(
        validate_grants=lambda *args: None,
        sync_account=lambda *args, **kwargs: None,
        account_sync_cursor=lambda *args: 0,
    )
    store = administration.Administration(local_repo, payments=payments)
    with TestClient(private_app) as private:

        def transport(request):
            response = private.post(
                request.url.path, headers=dict(request.headers), content=request.content
            )
            return httpx.Response(response.status_code, json=response.json())

        verifier = RestrictedVerifier(grant, "unused", transport=httpx.MockTransport(transport))
        app = FastAPI()

        def origin(request):
            if request.headers.get("origin") != runtime.frontend_origin:
                raise HTTPException(403)

        app.state.auth = SimpleNamespace(check_origin=origin)
        app.state.saas_auth_repository = verifier

        async def access(request: Request):
            actor, _ = verifier.tenant_actor_session(
                request.cookies.get("saas_tenant_session", ""), str(runtime.company_id)
            )
            return Scope(
                {
                    "id": actor.auth_user_id,
                    "is_portal_admin": actor.kind == "platform_owner",
                    "role": "owner" if actor.kind == "platform_owner" else "manager",
                },
                (),
                None,
                (),
                (),
                actor=actor,
            )

        app.include_router(
            administration.create_admin_router(access, local_repo, SimpleNamespace(), store)
        )
        app.include_router(create_profile_router(access, local_repo))
        token, session = tokens[0]
        payload = {
            "request_id": str(uuid4()),
            "email": "routed@example.com",
            "password": "temporary-password",
            "display_name": "Routed",
            "active": True,
            "sections": ["transfers"],
            "warehouse_scope_mode": "selected",
            "warehouse_ids": [],
        }
        with TestClient(app, base_url=runtime.frontend_origin) as client:
            client.cookies.set("saas_tenant_session", token)
            headers = {"origin": runtime.frontend_origin, "x-csrf-token": session["csrf_token"]}
            result = client.post("/api/management/accounts", headers=headers, json=payload)
            assert result.status_code == 201, result.text
            employee = result.json()["id"]
            assert "temporary-password" not in result.text
            assert (
                client.post("/api/management/accounts", headers=headers, json=payload).status_code
                == 201
            )
            with pool.connection() as db:
                current = db.execute(
                    render(
                        "SELECT sections,revision,is_portal_admin FROM {analytics}.web_users "
                        "WHERE id=%s",
                        runtime,
                    ),
                    (employee,),
                ).fetchone()
                assert current == {
                    "sections": ["transfers"],
                    "revision": 2,
                    "is_portal_admin": False,
                }
            emp_token, emp_session = repo.tenant_login(
                "company0", payload["email"], payload["password"], "routed"
            )
            client.cookies.clear()
            client.cookies.set("saas_tenant_session", emp_token)
            changed = client.post(
                "/api/profile/password",
                headers={
                    "origin": runtime.frontend_origin,
                    "x-csrf-token": emp_session["csrf_token"],
                },
                json={
                    "current_password": payload["password"],
                    "new_password": "route-personal-password",
                },
            )
            assert changed.status_code == 200, changed.text
            assert "saas_tenant_session=" in changed.headers["set-cookie"]
            assert "access_token" not in changed.text and "csrf_token" in changed.json()
        verifier.client.close()
    pool.close()


def test_identity_target_rejects_privileged_login_masquerading(accounts):
    _, _, _, _, dsn, runtimes, _, _ = accounts
    target = IdentityTarget(
        runtimes[0], make_conninfo(dsn, options=f"-c role={runtimes[0].key}_identity_runtime")
    )
    with pytest.raises(ValueError, match="dedicated unprivileged login"):
        target.is_admin(uuid4())


@pytest.mark.parametrize("later_edit", [False, True, "profile-race"])
def test_creation_replay_repairs_payment_sync_without_overwriting_later_grant(
    accounts, monkeypatch, tmp_path, later_edit
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi import HTTPException
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from app.tenancy.payment_bootstrap import TenantPaymentAdministration
    from app.tenant_payments.store import PaymentStore
    from app.web import administration
    from app.web.repository import Scope

    _, repo, _, operator, dsn, runtimes, tokens, _ = accounts
    runtime = runtimes[0]
    central_body = body()
    result = create(accounts, central_body)
    actor = repo.tenant_actor_session(tokens[0][0], str(runtime.company_id))[0]
    pool = ConnectionPool(
        make_conninfo(dsn, user=runtime.database_role), kwargs={"row_factory": dict_row}, open=False
    )

    @contextmanager
    def connection():
        pool.open()
        with pool.connection() as db:
            yield db

    local = SimpleNamespace(
        runtime=runtime,
        _pool=pool,
        connection=connection,
        actor_scope=lambda who: Scope(
            {
                "id": who.auth_user_id,
                "role": "owner",
                "sections": [],
                "warehouse_scope_mode": "all",
            },
            (),
            None,
            (),
            (),
            actor=who,
        ),
    )
    monkeypatch.setattr(administration, "DB", sql.Identifier(runtime.analytics_schema).as_string())
    vaultdir = tmp_path / "payments"
    vaultdir.mkdir()
    payment_store = PaymentStore(
        runtime, make_conninfo(dsn, user=f"{runtime.key}_payments_runtime"), Vault(vaultdir)
    )

    class FailOnce(TenantPaymentAdministration):
        failed = False

        def sync_account(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise HTTPException(503, "Synthetic payment outage")
            return super().sync_account(*args, **kwargs)

    payments = FailOnce(SimpleNamespace(store=payment_store), local)
    store = administration.Administration(local, payments)
    payload = administration.NewAccount(
        request_id=central_body["request_id"],
        email=central_body["email"],
        password=central_body["password"],
        display_name=central_body["display_name"],
        sections=["deposits"],
        warehouse_scope_mode="selected",
        warehouse_ids=[],
        deposits_all=True,
        deposits_create=True,
    )
    try:
        if later_edit == "profile-race":
            original_save = store.save_account

            def competing_save(who, user, edited, **kwargs):
                other = administration.EditAccount(
                    **{
                        **edited.model_dump(),
                        "display_name": "Later admin decision",
                        "deposits_create": False,
                    }
                )
                with pytest.raises(HTTPException) as interrupted:
                    original_save(who, user, other)
                assert interrupted.value.status_code == 503
                return original_save(who, user, edited, **kwargs)

            monkeypatch.setattr(store, "save_account", competing_save)
            store.save_identity_account(actor, UUID(result["id"]), payload)
            assert (
                operator.execute(
                    render("SELECT display_name FROM {analytics}.web_users", runtime)
                ).fetchone()[0]
                == "Later admin decision"
            )
            assert (
                operator.execute(
                    render("SELECT count(*) FROM {payments}.deposit_grants", runtime)
                ).fetchone()[0]
                == 0
            )
            assert operator.execute(
                render(
                    "SELECT state,profile_applied FROM {analytics}.portal_account_initializations",
                    runtime,
                )
            ).fetchone() == ("superseded", False)
            return
        with pytest.raises(HTTPException) as failed:
            store.save_identity_account(actor, UUID(result["id"]), payload)
        assert failed.value.status_code == 503
        assert (
            operator.execute(
                render("SELECT revision FROM {analytics}.web_users", runtime)
            ).fetchone()[0]
            == 2
        )
        assert (
            operator.execute(
                render("SELECT state FROM {analytics}.portal_account_initializations", runtime)
            ).fetchone()[0]
            == "pending"
        )
        if later_edit:
            payment_store.grant(
                payments.principal(actor),
                UUID(result["id"]),
                "*",
                is_all=True,
                can_create=False,
                profile_revision=2,
            )
        assert store.save_identity_account(actor, UUID(result["id"]), payload)["id"] == result["id"]
        row = operator.execute(
            render(
                "SELECT can_create,profile_revision FROM {payments}.deposit_grants "
                "WHERE user_id=%s",
                runtime,
            ),
            (result["id"],),
        ).fetchone()
        assert row == (not later_edit, 2)
        assert (
            operator.execute(
                render("SELECT state FROM {analytics}.portal_account_initializations", runtime)
            ).fetchone()[0]
            == "done"
        )
        assert store.save_identity_account(actor, UUID(result["id"]), payload)["already_created"]
    finally:
        pool.close()


@pytest.mark.parametrize("rejection_code,status", [("auth_rejected", 409), ("rate_limited", 429)])
def test_definite_rejection_allows_new_request_but_unknown_never_reposts(
    accounts, monkeypatch, rejection_code, status
):
    _, _, auth, operator, _, _, _, _ = accounts
    original = auth.create_user
    payload = body()

    def rejected(*args):
        auth.creates += 1
        raise Problem(status, rejection_code, "Synthetic policy rejection")

    monkeypatch.setattr(auth, "create_user", rejected)
    with pytest.raises(Problem):
        create(accounts, payload)
    assert (
        operator.execute("SELECT state FROM restcontrol.company_account_requests").fetchone()[0]
        == "rejected"
    )
    monkeypatch.setattr(auth, "create_user", original)
    assert create(
        accounts, {**payload, "request_id": str(uuid4()), "password": "corrected-password"}
    )["id"]
    assert auth.creates == 2
    unknown = body("unknown@example.com")

    def ambiguous(*args):
        auth.creates += 1
        raise Problem(503, "auth_ambiguous", "Synthetic transport outage")

    monkeypatch.setattr(auth, "create_user", ambiguous)
    for _ in range(2):
        with pytest.raises(Problem) as pending:
            create(accounts, unknown)
        assert pending.value.status == 503
    assert auth.creates == 3
    auth.register(unknown["email"], unknown["password"], unknown["request_id"])
    assert create(accounts, unknown)["id"] and auth.creates == 3
