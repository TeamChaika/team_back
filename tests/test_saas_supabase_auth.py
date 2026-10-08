"""Synthetic GoTrue transport checks: no network, secrets, or account changes."""

import httpx
import pytest

from app.saas_admin.repository import Problem
from app.saas_admin.supabase_auth import SupabaseAuthClient


def client(handler):
    return SupabaseAuthClient(
        "https://auth.example/auth/v1", "anon", "service", transport=httpx.MockTransport(handler)
    )


def test_password_grant_uses_anon_and_no_token_exposure():
    def handle(request):
        assert request.url.path == "/auth/v1/token"
        assert request.url.query == b"grant_type=password"
        assert request.headers["apikey"] == "anon"
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "user": {"id": "user"},
            },
        )

    pair = client(handle).login("a@example.com", "not-real")
    assert pair["user_id"] == "user"
    assert "password" not in pair


def test_creation_has_durable_marker_and_never_retries():
    calls = []

    def handle(request):
        calls.append(request)
        assert b"restcontrol_request_id" in request.content
        assert request.headers["apikey"] == "service"
        raise httpx.ReadTimeout("secret upstream detail")

    with pytest.raises(Problem) as error:
        client(handle).create_user("a@example.com", "secret", "marker")
    assert len(calls) == 1
    assert error.value.code == "auth_ambiguous"
    assert "secret" not in error.value.message


@pytest.mark.parametrize("status", [400, 401, 422])
def test_upstream_error_sanitized(status):
    with pytest.raises(Problem) as error:
        client(lambda _: httpx.Response(status, json={"message": "secret"})).login("x", "y")
    assert error.value.status == 401
    assert "secret" not in error.value.message


def test_no_redirects_or_invalid_tokens():
    with pytest.raises(Problem):
        client(lambda _: httpx.Response(302, headers={"location": "https://evil.example"})).user(
            "access"
        )
    with pytest.raises(Problem):
        client(lambda _: httpx.Response(200, json={"access_token": "x"})).login("x", "y")


def test_plain_http_only_loopback():
    with pytest.raises(ValueError):
        SupabaseAuthClient("http://auth.example/auth/v1", "a", "b")


@pytest.fixture
def pg_repo(tmp_path):
    from contextlib import contextmanager
    from pathlib import Path
    from uuid import uuid4

    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from app.saas_admin.pg_auth import PostgresAuth
    from app.saas_admin.pg_tenant_access import PostgresTenantAccess
    from app.saas_admin.vault import Vault

    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path / "postgres", cleanup_mode="stop")

    class Auth:
        refresh_calls = 0
        user_id = "00000000-0000-0000-0000-000000000001"

        def login(self, email, password):
            return {
                "access_token": "access",
                "refresh_token": "refresh",
                "user_id": self.user_id,
                "expires_at": 9999999999,
            }

        def user(self, token):
            return {"id": self.user_id}

        def refresh(self, token):
            self.refresh_calls += 1
            return self.login("", "")

        def create_user(self, *args):
            raise AssertionError("existing identity must not create account")

        def reset_password(self, *args):
            raise AssertionError("shared identity must not reset password")

    class Repo(PostgresAuth, PostgresTenantAccess):
        auth = Auth()

        @contextmanager
        def connect(self, write=False):
            with psycopg.connect(server.get_uri(), row_factory=dict_row) as db:
                db.execute("SET search_path=restcontrol,pg_catalog")
                yield db

        @staticmethod
        def _require_owner(db, actor):
            assert db.execute(
                "SELECT 1 FROM platform_memberships WHERE id=%s AND active", (actor["id"],)
            ).fetchone()

        @staticmethod
        def _get(db, company_id):
            return db.execute(
                "SELECT body FROM companies WHERE id=%s FOR UPDATE", (company_id,)
            ).fetchone()["body"]

    repo = Repo()
    repo.vault = Vault(tmp_path)
    repo.company_id, repo.owner_id = str(uuid4()), str(uuid4())
    with psycopg.connect(server.get_uri()) as db:
        db.execute("CREATE SCHEMA restcontrol")
        db.execute(
            "CREATE TABLE restcontrol.companies(id uuid primary key,slug text,status "
            "text,archived_at text,version integer,body jsonb)"
        )
        db.execute(Path("app/saas_admin/auth_schema.sql").read_text())
        db.execute("CREATE TABLE restcontrol.auth_identities(id uuid,email text,provision_id text)")
        db.execute(
            "CREATE TABLE restcontrol.events(id uuid,company_id uuid,actor_id uuid,actor_name "
            "text,action text,changed_fields jsonb,created_at text)"
        )
        body = {
            "id": repo.company_id,
            "slug": "tenant",
            "status": "draft",
            "archived_at": None,
            "version": 1,
            "name": "Tenant",
            "modules": [],
            "primary_admin": {"email": "existing@example.com", "name": "Admin"},
        }
        db.execute(
            "INSERT INTO restcontrol.companies VALUES (%s,%s,%s,NULL,1,%s)",
            (repo.company_id, "tenant", "draft", Jsonb(body)),
        )
        db.execute(
            "INSERT INTO restcontrol.auth_identities VALUES (%s,%s,NULL)",
            (repo.auth.user_id, "existing@example.com"),
        )
        db.execute(
            "INSERT INTO restcontrol.platform_memberships VALUES (%s,%s,%s,%s,true)",
            (repo.owner_id, repo.auth.user_id, "existing@example.com", "Owner"),
        )
    try:
        yield repo
    finally:
        server.cleanup()


def test_existing_provision_links_without_password(pg_repo):
    result = pg_repo.provision_admin(
        pg_repo.company_id, 1, {"id": pg_repo.owner_id, "display_name": "Owner"}
    )
    assert result["existing_account"] is True
    assert result["temporary_password"] is None
    assert result["admin"]["must_change_password"] is False
    with pytest.raises(Problem) as error:
        pg_repo.provision_admin(pg_repo.company_id, 2, {}, reset=True)
    assert error.value.code == "shared_identity"


def test_membership_revocation_and_wrong_tenant(pg_repo):
    pg_repo.provision_admin(
        pg_repo.company_id, 1, {"id": pg_repo.owner_id, "display_name": "Owner"}
    )
    token, _ = pg_repo.tenant_login("tenant", "existing@example.com", "fake", "peer")
    assert pg_repo.tenant_workspace(token, "tenant")["company"]["id"] == pg_repo.company_id
    with pytest.raises(Problem):
        pg_repo.tenant_session(token, "foreign")
    with pg_repo.connect(True) as db:
        db.execute("UPDATE memberships SET active=false")
    with pytest.raises(Problem):
        pg_repo.tenant_session(token, "tenant")


def test_refresh_persisted_then_identity_verified(pg_repo):
    import json


    token, _ = pg_repo.login("existing@example.com", "fake", "peer")
    with pg_repo.connect(True) as db:
        row = db.execute("SELECT tokens_ciphertext FROM sessions").fetchone()
        pair = json.loads(pg_repo.vault.decrypt(row["tokens_ciphertext"]))
        pair["expires_at"] = 0
        db.execute(
            "UPDATE sessions SET tokens_ciphertext=%s", (pg_repo.vault.encrypt(json.dumps(pair)),)
        )
    assert pg_repo.session(token)["user"]["id"] == pg_repo.owner_id
    assert pg_repo.auth.refresh_calls == 1
    pg_repo.auth.user_id = "00000000-0000-0000-0000-000000000002"
    with pytest.raises(Problem):
        pg_repo.session(token)


def test_ambiguous_refresh_revokes_local_session(pg_repo):
    import json

    token, _ = pg_repo.login("existing@example.com", "fake", "peer")
    with pg_repo.connect(True) as db:
        row = db.execute("SELECT tokens_ciphertext FROM sessions").fetchone()
        pair = json.loads(pg_repo.vault.decrypt(row["tokens_ciphertext"]))
        pair["expires_at"] = 0
        db.execute(
            "UPDATE sessions SET tokens_ciphertext=%s", (pg_repo.vault.encrypt(json.dumps(pair)),)
        )
    calls = []

    def ambiguous(_):
        calls.append(1)
        raise Problem(503, "auth_ambiguous", "Unavailable")

    pg_repo.auth.refresh = ambiguous
    for _ in range(2):
        with pytest.raises(Problem):
            pg_repo.session(token)
    assert len(calls) == 1
    with pg_repo.connect() as db:
        assert db.execute("SELECT 1 FROM sessions").fetchone() is None


def test_pending_creation_recovers_password_without_new_request(pg_repo):
    from uuid import uuid4

    marker = str(uuid4())
    with pg_repo.connect(True) as db:
        db.execute("UPDATE auth_identities SET provision_id=%s", (marker,))
        db.execute(
            "INSERT INTO auth_provisioning VALUES (%s,%s,%s,%s,NULL,%s,%s)",
            (
                pg_repo.company_id,
                marker,
                "existing@example.com",
                "pending",
                "2026-10-08",
                pg_repo.vault.encrypt("synthetic-temporary"),
            ),
        )
    result = pg_repo.provision_admin(
        pg_repo.company_id, 1, {"id": pg_repo.owner_id, "display_name": "Owner"}
    )
    assert result["temporary_password"] == "synthetic-temporary"
    assert result["admin"]["must_change_password"] is True
    with pg_repo.connect() as db:
        row = db.execute("SELECT state,temporary_ciphertext FROM auth_provisioning").fetchone()
        assert row == {"state": "complete", "temporary_ciphertext": None}


def test_concurrent_password_changes_serialize_before_auth(pg_repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    pg_repo.provision_admin(
        pg_repo.company_id, 1, {"id": pg_repo.owner_id, "display_name": "Owner"}
    )
    first, body1 = pg_repo.tenant_login("tenant", "existing@example.com", "old-password", "peer")
    second, body2 = pg_repo.tenant_login("tenant", "existing@example.com", "old-password", "peer")
    entered, release = Event(), Event()
    changes = []

    def change(_access, password):
        changes.append(password)
        entered.set()
        assert release.wait(5)
        return {"id": pg_repo.auth.user_id}

    pg_repo.auth.password = change

    def perform(token, csrf):
        try:
            pg_repo.tenant_password(token, "tenant", "old-password", "new-password", csrf, "peer")
            return "success"
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(perform, first, body1["csrf_token"])
        assert entered.wait(5)
        b = pool.submit(perform, second, body2["csrf_token"])
        release.set()
        assert sorted([a.result(10), b.result(10)]) == ["success", "unauthorized"]
    assert changes == ["new-password"]
