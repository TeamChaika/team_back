"""Two actual portal subprocesses + restricted DB roles + actual private verifier.

Auth provider is synthetic at its HTTP boundary; central sessions/SSO/memberships
and portal repository/document reads are real. This test does not assert external
DNS, iiko, terminal/provider or full business acceptance readiness.
"""

import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_tenant_migrations_postgres import empty_database as empty_database

from app.saas_admin.entitlements import FEATURES
from app.saas_admin.pg_auth import PostgresAuth
from app.saas_admin.pg_tenant_access import PostgresTenantAccess
from app.saas_admin.platform_sso import pkce_challenge
from app.saas_admin.runtime_registry import SETUP_CHECKS, ReadyRuntime, RuntimeRegistry
from app.saas_admin.server import create_app
from app.saas_admin.supabase_auth import SupabaseAuthClient
from app.saas_admin.vault import Vault
from app.tenancy.bootstrap import VerifierGrant, create_verifier_app
from app.tenancy.migrations import provision_tenant
from app.tenancy.sql import render


@contextmanager
def verifier_server(app, socket_path):
    server = uvicorn.Server(
        uvicorn.Config(app, uds=str(socket_path), access_log=False, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.025)
    assert server.started
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(5)


def auth_client(owner, member):
    def handler(request):
        if request.url.path.endswith("/token"):
            uid = (
                owner if json.loads(request.content).get("email") == "owner@example.org" else member
            )
            return httpx.Response(
                200,
                json={
                    "access_token": str(uid),
                    "refresh_token": str(uid),
                    "expires_in": 3600,
                    "user": {"id": str(uid)},
                },
            )
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": request.headers["authorization"].split()[-1]})
        raise AssertionError(request.url.path)

    return SupabaseAuthClient(
        "https://auth.example/auth/v1", "anon", "operator", transport=httpx.MockTransport(handler)
    )


def test_two_real_portals_owner_member_and_revocation(empty_database):
    operator, dsn, tenant = empty_database
    owner, member, product, store = uuid4(), uuid4(), uuid4(), uuid4()
    with tempfile.TemporaryDirectory(prefix="rcp-", dir="/tmp") as root_name:
        root = Path(root_name)
        root.chmod(0o700)
        runtimes = [
            replace(
                tenant(),
                frontend_origin=f"https://client{i}.example.org",
                api_origin=f"https://api.client{i}.example.org",
            )
            for i in (1, 2)
        ]
        runtimes = [replace(r, runtime_directory=root / r.key) for r in runtimes]
        operator.execute("CREATE SCHEMA restcontrol")
        operator.execute(
            "CREATE TABLE restcontrol.companies(id uuid PRIMARY KEY,slug text,status text,ar"
            "chived_at text,version int,body jsonb)"
        )
        operator.execute(Path("app/saas_admin/auth_schema.sql").read_text())
        operator.execute(
            "CREATE TABLE restcontrol.auth_identities(id uuid,email text,provision_id text)"
        )
        for i, runtime in enumerate(runtimes, 1):
            provision_tenant(operator, runtime)
            body = {
                "id": str(runtime.company_id),
                "name": f"Client {i}",
                "slug": f"client{i}",
                "domain": f"client{i}.example.org",
                "version": 1,
                "status": "active",
                "archived_at": None,
                "modules": {"analytics": True, "documents": True, "deposits": True},
                "subscription": {
                    "policy": "plans_v1",
                    "plan_id": "full",
                    "status": "active",
                    "start_date": "2020-01-01",
                },
            }
            operator.execute(
                "INSERT INTO restcontrol.companies VALUES(%s,%s,%s,NULL,1,%s)",
                (runtime.company_id, body["slug"], "active", Jsonb(body)),
            )
            operator.execute(
                "INSERT INTO restcontrol.memberships(id,company_id,auth_user_id,username,dis"
                "play_name) VALUES(%s,%s,%s,%s,%s)",
                (uuid4(), runtime.company_id, member, "member@example.org", f"Member {i}"),
            )
            operator.execute(
                render(
                    "INSERT INTO {analytics}.sources(id,label,base_url,fingerprint) VALUES('"
                    "primary',%s,'https://iiko.example','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaa')",
                    runtime,
                ),
                (f"Company {i}",),
            )
            operator.execute(
                render(
                    "INSERT INTO {analytics}.web_users(id,display_name,role,sections,is_port"
                    "al_admin,all_departments,password_change_required) VALUES(%s,%s,'owner'"
                    ",ARRAY['products','transfers','writeoffs'],true,true,false)",
                    runtime,
                ),
                (member, f"Member {i}"),
            )
            operator.execute(
                render(
                    "INSERT INTO {analytics}.portal_identity_metadata(id,email) VALUES(%s,'m"
                    "ember@example.org')",
                    runtime,
                ),
                (member,),
            )
            run_id, snapshot = uuid4(), uuid4()
            operator.execute(
                render(
                    "INSERT INTO {analytics}.sync_runs(id,status,finished_at) VALUES(%s,'suc"
                    "ceeded',now())",
                    runtime,
                ),
                (run_id,),
            )
            operator.execute(
                render(
                    "INSERT INTO {analytics}.raw_snapshots VALUES(%s,%s,'primary','products'"
                    ",now(),encode(sha256(''::bytea),'hex'),0,''::bytea,'{{}}')",
                    runtime,
                ),
                (snapshot, run_id),
            )
            operator.execute(
                render(
                    "INSERT INTO {analytics}.corporate_nodes(source_id,id,name,type,first_se"
                    "en_at,last_seen_at,last_snapshot_id) VALUES('primary',%s,%s,'DEPARTMENT"
                    "',now(),now(),%s)",
                    runtime,
                ),
                (store, f"Company department {i}", snapshot),
            )
            operator.execute(
                render(
                    "INSERT INTO {analytics}.products(source_id,id,name,type,main_unit_id,de"
                    "leted,default_sale_price,unit_weight,unit_capacity,details,first_seen_a"
                    "t,last_seen_at,last_snapshot_id) VALUES('primary',%s,%s,'DISH',%s,false"
                    ",100,1,1,'{{}}',now(),now(),%s)",
                    runtime,
                ),
                (product, f"Only company {i}", uuid4(), snapshot),
            )
            operator.execute(
                render(
                    "INSERT INTO {documents}.authentication_user(id,password,is_superuser,us"
                    "ername,first_name,last_name,email,is_staff,is_active,date_joined) VALUE"
                    "S(1,'!',false,'same-user','','','',false,true,now())",
                    runtime,
                )
            )
            operator.execute(
                render("INSERT INTO {documents}.stores(id,name) VALUES(%s,%s)", runtime),
                (store, f"Store {i}"),
            )
            operator.execute(
                render(
                    "INSERT INTO {documents}.waybills(id,comment,status,created_at,counterag"
                    "ent_id,created_by_id,store_id,submission_state,version) VALUES(1,%s,'Cr"
                    "eated',now(),%s,1,%s,'draft',1)",
                    runtime,
                ),
                (f"Only document {i}", store, store),
            )
        operator.execute(
            "INSERT INTO restcontrol.platform_memberships VALUES(%s,%s,'owner@example.org','"
            "Global Owner',true)",
            (uuid4(), owner),
        )
        operator.execute(
            "INSERT INTO restcontrol.auth_identities VALUES(%s,%s,NULL)",
            (owner, "owner@example.org"),
        )
        operator.execute(
            "INSERT INTO restcontrol.auth_identities VALUES(%s,%s,NULL)",
            (member, "member@example.org"),
        )

        class RegistryRepo(PostgresAuth, PostgresTenantAccess):
            @contextmanager
            def connect(self, write=False):
                with psycopg.connect(dsn, row_factory=dict_row) as db:
                    db.execute("SET LOCAL search_path TO restcontrol,pg_catalog")
                    yield db

            @staticmethod
            def _get(db, company_id):
                return db.execute(
                    "SELECT body FROM companies WHERE id=%s", (company_id,)
                ).fetchone()["body"]

            def get(self, company_id):
                with self.connect() as db:
                    return self._get(db, company_id)

            def company_for_domain(self, domain):
                with self.connect() as db:
                    row = db.execute(
                        "SELECT body FROM companies WHERE body->>'domain'=%s AND status='active'",
                        (domain,),
                    ).fetchone()
                    return row["body"] if row else None

        (root / "control").mkdir(mode=0o700)
        repo = RegistryRepo()
        repo.auth = auth_client(owner, member)
        repo.vault = Vault(root / "control")
        grants = [
            VerifierGrant(str(r.company_id), "portal", secrets.token_urlsafe(32)) for r in runtimes
        ]
        verifier_path = root / "verifier.sock"
        processes = []
        logs = []
        try:
            with verifier_server(create_verifier_app(repo, grants), verifier_path):
                for runtime, grant in zip(runtimes, grants, strict=True):
                    runtime.runtime_directory.mkdir(mode=0o700)
                    secret_file = runtime.runtime_path("verifier.key")
                    secret_file.write_text(grant.secret)
                    secret_file.chmod(0o600)
                    env = {
                        "RESTCONTROL_RUNTIME_MODE": "tenant",
                        "RESTCONTROL_TENANT_COMPANY_ID": str(runtime.company_id),
                        "RESTCONTROL_TENANT_TIMEZONE": "UTC",
                        "RESTCONTROL_TENANT_FRONTEND_ORIGIN": runtime.frontend_origin,
                        "RESTCONTROL_TENANT_API_ORIGIN": runtime.api_origin,
                        "RESTCONTROL_TENANT_RUNTIME_DIRECTORY": str(runtime.runtime_directory),
                        "RESTCONTROL_TENANT_CONFIGURATION_VERSION": "1",
                        "RESTCONTROL_TENANT_DATABASE_ROLE": runtime.database_role,
                        "RESTCONTROL_TENANT_PAYMENTS_DATABASE_URL": make_conninfo(
                            dsn, user=f"{runtime.key}_payments_runtime"
                        ),
                        "RESTCONTROL_TENANT_DATABASE_URL": make_conninfo(
                            dsn, user=runtime.database_role
                        ),
                        "RESTCONTROL_TENANT_IIKO_BASE_URL": "https://iiko.example/resto/api",
                        "RESTCONTROL_TENANT_IIKO_LOGIN": "test",
                        "RESTCONTROL_TENANT_IIKO_PASSWORD": "test",
                        "RESTCONTROL_TENANT_WEB_SUPABASE_URL": "https://auth.example",
                        "RESTCONTROL_TENANT_WEB_ANON_KEY": "anon",
                        "RESTCONTROL_TENANT_SYNC_ENABLED": "false",
                        "RESTCONTROL_TENANT_LIVE_SALES_ENABLED": "false",
                        "RESTCONTROL_TENANT_WEB_DOCUMENTS_ENABLED": "true",
                        "RESTCONTROL_TENANT_DOCUMENTS_NATIVE_ENABLED": "true",
                        "RESTCONTROL_TENANT_DOCUMENTS_DATABASE_URL": make_conninfo(
                            dsn, user=runtime.database_role
                        ),
                        "RESTCONTROL_TENANT_DOCUMENTS_IIKO_URL": "https://iiko.example",
                        "RESTCONTROL_TENANT_DOCUMENTS_IIKO_LOGIN": "test",
                        "RESTCONTROL_TENANT_DOCUMENTS_IIKO_PASSWORD_HASH": "a" * 40,
                        "RESTCONTROL_TENANT_VERIFIER_SECRET_FILE": str(secret_file),
                        "RESTCONTROL_TENANT_VERIFIER_SOCKET": str(verifier_path),
                    }
                    config = runtime.runtime_path("env.json")
                    config.write_text(json.dumps(env))
                    config.chmod(0o600)
                    log = open(runtime.runtime_path("test.log"), "w+")
                    logs.append(log)
                    process = subprocess.Popen(
                        [sys.executable, "-m", "app.tenancy.bootstrap", "--config", str(config)],
                        stdout=log,
                        stderr=log,
                    )
                    processes.append(process)
                    socket_path = runtime.runtime_path("portal.sock")
                    for _ in range(300):
                        if socket_path.exists() or process.poll() is not None:
                            break
                        time.sleep(0.025)
                    if not socket_path.exists():
                        log.seek(0)
                        pytest.fail(log.read())

                # Persist setup prerequisites; external iiko/DNS/payment acceptance is NOT claimed.
                operator.execute(
                    "CREATE TABLE restcontrol.runtime_provisioning(company_id uuid PRIMARY KEY,"
                    "configuration_version int,socket_path text,checks jsonb,state text,"
                    "step text,error_code text,updated_at timestamptz)"
                )
                for runtime in runtimes:
                    operator.execute(
                        "INSERT INTO restcontrol.runtime_provisioning VALUES(%s,1,%s,%s,'failed')",
                        (
                            runtime.company_id,
                            str(runtime.runtime_path("portal.sock")),
                            Jsonb(
                                {
                                    key: {"ok": True, "evidence": "fixture prerequisites"}
                                    for key in SETUP_CHECKS
                                }
                            ),
                        ),
                    )
                setup_registry = RuntimeRegistry(repo)

                # Explicit test-only route registry: external readiness is NOT asserted.
                class RunningProcesses:
                    setup_only = False

                    def feature_readiness(self, company):
                        # Explicit fixture permission only, not external acceptance evidence.
                        return {
                            feature: {"read": not self.setup_only, "write": not self.setup_only}
                            for feature in FEATURES
                        }

                    def payment_configuration_change(self, company):
                        return setup_registry.payment_configuration_change(company)

                    def resolve_setup(self, company):
                        return setup_registry.resolve_setup(company)

                    def resolve(self, company):
                        if self.setup_only:
                            return None
                        runtime = next(r for r in runtimes if str(r.company_id) == company["id"])
                        return ReadyRuntime(
                            company["id"], 1, str(runtime.runtime_path("portal.sock"))
                        )

                data = root / "gateway"
                data.mkdir(mode=0o700)
                Vault(data)
                dist = root / "dist"
                (dist / "assets").mkdir(parents=True)
                (dist / "saas-admin.html").write_text("test")
                running = RunningProcesses()
                gateway = create_app(
                    data,
                    dist,
                    "https://rc.example.org",
                    "production",
                    repository=repo,
                    runtime_registry=running,
                )
                parent, _ = repo.login("owner@example.org", "synthetic", "owner-peer")
                tokens = []
                with TestClient(gateway) as client:
                    for i, runtime in enumerate(runtimes, 1):
                        verifier, state, nonce = [secrets.token_urlsafe(32) for _ in range(3)]
                        code = repo.authorize_platform(
                            parent, str(runtime.company_id), state, nonce, pkce_challenge(verifier)
                        )
                        token, owner_session = repo.exchange_platform(
                            code["code"],
                            verifier,
                            state,
                            nonce,
                            f"client{i}",
                            runtime.frontend_origin,
                            runtime.api_origin,
                        )
                        tokens.append(token)
                        # Real acceptance probes: own session + actual private portal process.
                        from types import SimpleNamespace

                        from app.saas_admin.provisioning import PendingCheck
                        from app.saas_admin.runtime_acceptance import check_modules

                        session_file = runtime.runtime_path("acceptance-session")
                        session_file.write_text(token)
                        session_file.chmod(0o600)
                        acceptance = SimpleNamespace(
                            runtime=runtime,
                            repo=repo,
                            config={
                                "acceptance_session_file": str(session_file),
                                "runtime_dsn": make_conninfo(dsn, user=runtime.database_role),
                                "history_from": "2026-10-01",
                                "history_to": "2026-10-01",
                            },
                            current=lambda company_id, version: repo.get(company_id),
                            launch=lambda role: None,  # Already running actual company process.
                        )
                        try:
                            module_evidence = check_modules(acceptance)["evidence"]
                        except PendingCheck as pending:
                            assert pending.code == "module_configuration_required"
                            module_evidence = pending.details
                            assert set(module_evidence["missing"]) <= {
                                "telegram_bot_required",
                                "documents_worker_not_ready",
                                "scheduler_not_ready",
                                "/api/assistant/status",
                                "assistant_configured_not_ready",
                                "telegram_identity_not_ready",
                            }, module_evidence
                        passed = {
                            probe["path"] for probe in module_evidence["probes"] if probe["ok"]
                        }
                        assert {
                            "/api/me",
                            "/api/resources/products",
                            "/api/documents/waybill",
                            "/api/documents/waybill/export",
                        } <= passed, module_evidence
                        original_company = repo.get(str(runtime.company_id))
                        selected_company = json.loads(json.dumps(original_company))
                        selected_company["subscription"]["overrides"] = {
                            feature: {"mode": "deny"}
                            for feature in (
                                "notifications.telegram",
                                "assistant.chat",
                                "documents.dispatch",
                                "operations.sync",
                            )
                        }
                        with repo.connect(True) as db:
                            db.execute(
                                "UPDATE companies SET body=%s WHERE id=%s",
                                (Jsonb(selected_company), runtime.company_id),
                            )
                        try:
                            accepted = check_modules(acceptance)
                            assert accepted["ok"] is True
                            assert all(probe["ok"] for probe in accepted["evidence"]["probes"])
                        finally:
                            with repo.connect(True) as db:
                                db.execute(
                                    "UPDATE companies SET body=%s WHERE id=%s",
                                    (Jsonb(original_company), runtime.company_id),
                                )
                        headers = {
                            "origin": runtime.frontend_origin,
                            "cookie": "saas_tenant_session=" + token,
                        }
                        me = client.get(runtime.api_origin + "/api/me", headers=headers)
                        assert me.status_code == 200, me.text
                        assert me.json()["user"]["display_name"] == "Global Owner"
                        products = client.get(
                            runtime.api_origin + "/api/resources/products", headers=headers
                        )
                        assert products.status_code == 200, products.text
                        assert (
                            f"Only company {i}" in products.text
                            and f"Only company {3 - i}" not in products.text
                        )
                        docs = client.get(
                            runtime.api_origin + "/api/documents/waybill", headers=headers
                        )
                        assert docs.status_code == 200, docs.text
                        assert (
                            f"Only document {i}" in docs.text
                            and f"Only document {3 - i}" not in docs.text
                        )
                        for route in ("/api/deposits/permissions", "/api/management/venues"):
                            payment_reply = client.get(runtime.api_origin + route, headers=headers)
                            assert payment_reply.status_code == 200, payment_reply.text
                        running.setup_only = True
                        context = client.get(
                            runtime.api_origin + "/api/saas-context", headers=headers
                        )
                        assert context.json()["setup_available"] is True, context.text
                        assert context.json()["full_dashboard_ready"] is False
                        assert setup_registry.resolve(repo.get(str(runtime.company_id))) is None
                        assert (
                            client.get(
                                runtime.api_origin + "/api/resources/products", headers=headers
                            ).status_code
                            == 403
                        )
                        setup_member, _ = repo.tenant_login(
                            f"client{i}", "member@example.org", "synthetic", f"setup-member-{i}"
                        )
                        assert (
                            client.get(
                                runtime.api_origin + "/api/management/venues",
                                headers={
                                    **headers,
                                    "cookie": "saas_tenant_session=" + setup_member,
                                },
                            ).status_code
                            == 403
                        )
                        venue, terminal = str(uuid4()), str(uuid4())
                        write_headers = {**headers, "x-csrf-token": owner_session["csrf_token"]}
                        venue_reply = client.post(
                            runtime.api_origin + "/api/payment-settings/venues/" + venue,
                            headers=write_headers,
                            json={"name": "Same venue"},
                        )
                        assert venue_reply.status_code == 200, venue_reply.text
                        terminal_reply = client.post(
                            runtime.api_origin
                            + f"/api/payment-settings/venues/{venue}/terminals/{terminal}",
                            headers=write_headers,
                            json={
                                "name": "Test terminal",
                                "api_key": "synthetic-key-no-network",
                                "mode": "sandbox",
                                "make_default": True,
                            },
                        )
                        assert terminal_reply.status_code == 200, terminal_reply.text
                        from app.saas_admin.runtime_operator import RuntimeOperator

                        payment_operator = RuntimeOperator.__new__(RuntimeOperator)
                        payment_operator.runtime = runtime
                        payment_operator.company = repo.get(str(runtime.company_id))
                        payment_operator.config = {
                            "payments_dsn": make_conninfo(
                                dsn, user=f"{runtime.key}_payments_runtime"
                            )
                        }
                        with pytest.raises(PendingCheck) as payment_pending:
                            payment_operator.payments()
                        assert payment_pending.value.code == "payment_terminal_check_required"
                        assert payment_pending.value.details["configured_terminals"] == 1
                        assert (
                            client.post(
                                runtime.api_origin + "/api/deposits", headers=write_headers, json={}
                            ).status_code
                            == 403
                        )
                        # Explicit fixture routing resumes; no production readiness is forged.
                        running.setup_only = False
                        created = client.post(
                            runtime.api_origin + "/api/deposits",
                            headers=write_headers,
                            json={
                                "request_id": str(uuid4()),
                                "customer_name": "Private guest",
                                "phone": "79999999999",
                                "amount": 100,
                                "restaurant": venue,
                            },
                        )
                        assert created.status_code == 201, created.text
                        from urllib.parse import urlsplit

                        guest_link = urlsplit(created.json()["guest_url"])
                        guest = client.get(
                            runtime.api_origin
                            + "/api/guest-deposits/"
                            + created.json()["id"]
                            + "?"
                            + guest_link.query,
                            headers={"origin": runtime.frontend_origin},
                        )
                        assert guest.status_code == 200, guest.text
                        assert "Private guest" not in guest.text and "79999999999" not in guest.text
                        other_guest = client.get(
                            runtimes[2 - i].api_origin
                            + "/api/guest-deposits/"
                            + created.json()["id"]
                            + "?"
                            + guest_link.query,
                            headers={"origin": runtimes[2 - i].frontend_origin},
                        )
                        assert other_guest.status_code == 404, other_guest.text
                        callback = client.post(
                            runtime.api_origin
                            + f"/api/payment-callbacks/{uuid4()}?token="
                            + "x" * 32,
                            json={"operation_id": str(uuid4())},
                        )
                        assert callback.status_code == 404, callback.text
                        # A fresh policy change must stop direct private first-POST work,
                        # even when an earlier gateway/queue check had allowed it.
                        current_company = repo.get(str(runtime.company_id))
                        expired = {
                            **current_company,
                            "subscription": {
                                **current_company["subscription"],
                                "end_date": "2020-02-01",
                            },
                        }
                        with repo.connect(True) as db:
                            db.execute(
                                "UPDATE companies SET body=%s WHERE id=%s",
                                (Jsonb(expired), runtime.company_id),
                            )
                        from urllib.parse import parse_qs

                        guest_token = parse_qs(guest_link.query)["token"][0]
                        with httpx.Client(
                            transport=httpx.HTTPTransport(
                                uds=str(runtime.runtime_path("portal.sock"))
                            ),
                            base_url="http://runtime",
                            trust_env=False,
                        ) as private:
                            denied = private.post(
                                "/api/guest-deposits/" + created.json()["id"] + "/prepare",
                                headers={
                                    "host": urlsplit(runtime.api_origin).netloc,
                                    "origin": runtime.frontend_origin,
                                },
                                json={"token": guest_token, "request_id": str(uuid4())},
                            )
                            assert denied.status_code == 403, denied.text
                        assert (
                            operator.execute(
                                render("SELECT count(*) FROM {payments}.attempts", runtime)
                            ).fetchone()[0]
                            == 0
                        )
                        refreshed = client.post(
                            runtime.api_origin
                            + "/api/guest-deposits/"
                            + created.json()["id"]
                            + "/reconcile",
                            headers={"origin": runtime.frontend_origin},
                            json={"token": guest_token, "request_id": str(uuid4())},
                        )
                        assert refreshed.status_code == 200, refreshed.text
                        with repo.connect(True) as db:
                            db.execute(
                                "UPDATE companies SET body=%s WHERE id=%s",
                                (Jsonb(current_company), runtime.company_id),
                            )
                        member_token, _ = repo.tenant_login(
                            f"client{i}", "member@example.org", "synthetic", f"member-{i}"
                        )
                        reply = client.get(
                            runtime.api_origin + "/api/me",
                            headers={**headers, "cookie": "saas_tenant_session=" + member_token},
                        )
                        assert reply.status_code == 200, reply.text
                        assert reply.json()["user"]["display_name"] == f"Member {i}"
                    assert (
                        client.get(
                            runtimes[1].api_origin + "/api/me",
                            headers={
                                "origin": runtimes[1].frontend_origin,
                                "cookie": "saas_tenant_session=" + tokens[0],
                            },
                        ).status_code
                        == 401
                    )
                    repo.logout(parent)
                    assert (
                        client.get(
                            runtimes[0].api_origin + "/api/me",
                            headers={
                                "origin": runtimes[0].frontend_origin,
                                "cookie": "saas_tenant_session=" + tokens[0],
                            },
                        ).status_code
                        == 401
                    )
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in logs:
                log.close()
            repo.auth.close()
