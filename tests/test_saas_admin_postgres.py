"""Real disposable PostgreSQL: migration, import fidelity and native registry CRUD."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app.saas_admin.import_registry import import_snapshot, read_snapshot
from app.saas_admin.models import CompanyWrite
from app.saas_admin.postgres_repository import PostgresRepository
from app.saas_admin.repository import Problem, Repository

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20261008135638_restcontrol_supabase_registry.sql"
)
OWNER_AUTH = str(uuid4())


@pytest.fixture(scope="module")
def pg_server():
    pgserver = pytest.importorskip("pgserver")
    with TemporaryDirectory(prefix="restcontrol-pg-test-") as directory:
        server = pgserver.get_server(Path(directory) / "postgres", cleanup_mode="stop")
        url = server.get_uri()
        try:
            with psycopg.connect(url, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE chaika_backend"
                )
                db.execute("CREATE SCHEMA auth; CREATE SCHEMA chaika")
                db.execute(
                    "CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,"
                    "raw_app_meta_data jsonb)"
                )
                db.execute("CREATE TABLE chaika.web_users(id uuid PRIMARY KEY,role text)")
                db.execute("INSERT INTO auth.users VALUES(%s,'1@chaika.team','{}')", (OWNER_AUTH,))
                db.execute("INSERT INTO chaika.web_users VALUES(%s,'owner')", (OWNER_AUTH,))
                db.execute(MIGRATION.read_text())
            yield url
        finally:
            server.cleanup()


@pytest.fixture
def database(pg_server):
    with psycopg.connect(pg_server, autocommit=True) as db:
        db.execute(
            "TRUNCATE restcontrol.companies,restcontrol.platform_memberships,"
            "restcontrol.attempts,restcontrol.check_targets,restcontrol.imports CASCADE"
        )
    return pg_server


@pytest.fixture
def imported(database, tmp_path):
    legacy = Repository(tmp_path / "registry")
    legacy.bootstrap("owner", "legacy-test-password-123", "Original Owner")
    with legacy.connect() as db:
        actor = dict(db.execute("SELECT * FROM owners").fetchone())
    rms_id = str(uuid4())
    fields = CompanyWrite(
        name="Первая компания",
        slug="company-one",
        domain="one.example.com",
        primary_admin={"name": "Admin", "email": "new@example.com", "phone": "+79990000000"},
        chain_url="https://one.iiko.it",
        rms=[{"id": rms_id, "label": "First RMS", "url": "https://rms.iiko.it", "enabled": True}],
    ).model_dump(mode="json")
    first = legacy.save(
        fields,
        actor,
        credentials={
            "chain": {"login": "api", "password": "secret-one"},
            rms_id: {"login": "rms-api", "password": "secret-three"},
        },
    )
    access = legacy.provision_admin(first["id"], first["version"], actor)
    legacy.login("owner", "legacy-test-password-123", "peer")
    token, session = legacy.tenant_login(
        "company-one", "new@example.com", access["temporary_password"], "peer"
    )
    legacy.tenant_password(
        token,
        "company-one",
        access["temporary_password"],
        "changed-synthetic-password",
        session["csrf_token"],
        "peer",
    )
    fields = CompanyWrite(
        name="Second", slug="company-two", chain_url="https://two.iiko.it"
    ).model_dump(mode="json")
    second = legacy.save(
        fields, actor, credentials={"chain": {"login": "api", "password": "secret-two"}}
    )
    manifest = import_snapshot(tmp_path / "registry", database, OWNER_AUTH)
    repo = PostgresRepository(
        make_conninfo(database, options="-c role=restcontrol_backend"), tmp_path / "registry"
    )
    return repo, legacy, actor, first, second, manifest


def test_import_fidelity_no_passwords_or_sessions(imported):
    repo, legacy, actor, first, second, manifest = imported
    assert manifest["status"] == "imported"
    assert manifest["counts"]["companies"] == 2
    assert manifest["counts"]["tenant_admins"] == 1
    assert manifest["counts"]["connections"] == 3
    assert manifest["counts"]["tenant_events"] == 1
    assert manifest["password_hashes_imported"] == manifest["sessions_imported"] == 0
    repo.validate_ready()
    assert repo.get(first["id"]) == legacy.get(first["id"])
    assert repo.listing("ПЕРВАЯ", None, 50, 0)["total"] == 1
    assert repo.get(second["id"]) == legacy.get(second["id"])
    assert repo.events(first["id"], 100, 0) == legacy.events(first["id"], 100, 0)
    with repo.connect() as db:
        member = db.execute("SELECT * FROM memberships").fetchone()
        assert member["auth_user_id"] is None
        assert not {"salt", "password_hash"}.intersection(member)
        assert db.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM tenant_sessions").fetchone()["n"] == 0
    assert repo.admin_access(first["id"])["admin"]["status"] == "activation_required"
    assert repo.check_inputs(second["id"], "chain", second["version"])[2] == "secret-two"


def test_import_idempotent_and_reject_wrong_owner(database, tmp_path):
    legacy = Repository(tmp_path / "snapshot")
    legacy.bootstrap("owner", "legacy-test-password-123", "Owner")
    with pytest.raises(ValueError, match="mismatch"):
        import_snapshot(tmp_path / "snapshot", database, uuid4())
    one = import_snapshot(tmp_path / "snapshot", database, OWNER_AUTH)
    two = import_snapshot(tmp_path / "snapshot", database, OWNER_AUTH)
    assert one["source_digest"] == two["source_digest"]
    assert two["status"] == "already_imported"
    data, summary = read_snapshot(tmp_path / "snapshot")
    assert "password_hash" not in data["owners"][0]
    assert summary["counts"]["owners"] == 1


def test_privileges_and_unrelated_dashboard(imported, database):
    repo, *_ = imported
    with repo.connect() as db:
        assert db.execute("SELECT current_user AS name").fetchone()["name"] == "restcontrol_backend"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM chaika.web_users")
    with repo.connect() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM auth.users")
    with repo.connect() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("DELETE FROM platform_memberships")
    with repo.connect() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("DELETE FROM events")
    with psycopg.connect(database) as db:
        assert (
            db.execute("SELECT role FROM chaika.web_users WHERE id=%s", (OWNER_AUTH,)).fetchone()[0]
            == "owner"
        )
        for role in ("anon", "authenticated", "chaika_backend"):
            assert not db.execute(
                "SELECT has_schema_privilege(%s,'restcontrol','USAGE')", (role,)
            ).fetchone()[0]
        assert db.execute(
            "SELECT bool_and(relrowsecurity) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='restcontrol' AND relkind='r'"
        ).fetchone()[0]


def test_native_save_race_audit_and_domain(imported):
    repo, _, actor, first, _, _ = imported
    initial = repo.get(first["id"])
    values = CompanyWrite(
        **{k: v for k, v in initial.items() if k in CompanyWrite.model_fields}
    ).model_dump(mode="json")

    def update(name):
        try:
            return repo.save({**values, "name": name}, actor, first["id"], initial["version"])
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["First update", "Second update"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "version_conflict" in results
    saved = repo.get(first["id"])
    assert saved["version"] == initial["version"] + 1
    assert repo.company_for_domain("one.example.com")["id"] == first["id"]
    assert repo.company_for_domain("evil.example.com") is None
    assert repo.listing("UPDATE", None, 50, 0)["total"] == 1
    suspended = repo.save({**values, "status": "suspended"}, actor, first["id"], saved["version"])
    assert repo.company_for_domain("one.example.com") is None
    assert suspended["status"] == "suspended"


def test_connection_update_check_and_archived_uniqueness(imported):
    repo, _, actor, first, second, _ = imported
    repo.reserve_check(actor["id"], "https://one.iiko.it")
    with pytest.raises(Problem) as err:
        repo.reserve_check(actor["id"], "https://one.iiko.it")
    assert err.value.code == "rate_limited"
    check = {"status": "ok", "code": None, "message": "ok", "checked_at": "2026-10-08T00:00:00Z"}
    result = repo.record_check(second["id"], "chain", 1, check, actor)
    assert result["company_version"] == 2
    assert repo.get(second["id"])["integration_state"] == "ok"
    values = CompanyWrite(
        name="Archived", slug="company-two", chain_url="https://two.iiko.it"
    ).model_dump(mode="json")
    repo.save(values, actor, second["id"], 2, archive=True)
    assert repo.events(second["id"], 10, 0)["total"] == 3
    with pytest.raises(Problem) as err:
        repo.save(CompanyWrite(name="Duplicate", slug="company-two").model_dump(mode="json"), actor)
    assert err.value.code == "duplicate"


def test_restricted_role_owner_and_shared_auth_membership(imported):
    repo, _, actor, _, _, _ = imported

    class Auth:
        def login(self, username, password):
            return {
                "access_token": "synthetic",
                "refresh_token": "synthetic-refresh",
                "expires_at": 9999999999,
                "user_id": OWNER_AUTH,
            }

        def user(self, token):
            return {"id": OWNER_AUTH}

        def create_user(self, *args):
            raise AssertionError("Existing Auth identity must not be recreated")

    repo.auth = Auth()
    token, session = repo.login("1@chaika.team", "synthetic", "peer")
    assert session["user"]["id"] == actor["id"]
    assert repo.session(token)["user"]["username"] == "1@chaika.team"
    company = repo.save(
        CompanyWrite(
            name="Shared",
            slug="shared-company",
            primary_admin={"name": "Shared Admin", "email": "1@chaika.team", "phone": ""},
        ).model_dump(mode="json"),
        actor,
    )
    access = repo.provision_admin(company["id"], company["version"], actor)
    assert access["existing_account"] and access["temporary_password"] is None
    tenant_token, _ = repo.tenant_login("shared-company", "1@chaika.team", "synthetic", "peer")
    assert repo.tenant_workspace(tenant_token, "shared-company")["company"]["id"] == company["id"]
    repo.logout(token)
    with pytest.raises(Problem):
        repo.session(token)
    # Owner logout does not silently revoke a different product/surface session.
    assert (
        repo.tenant_session(tenant_token, "shared-company")["user"]["company_id"] == company["id"]
    )


def test_audit_failure_rolls_back_company_and_secret(imported, database):
    repo, _, actor, _, second, _ = imported
    with psycopg.connect(database) as db:
        db.execute(
            "CREATE FUNCTION restcontrol.fail_audit() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$"
        )
        db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON restcontrol.events "
            "FOR EACH ROW EXECUTE FUNCTION restcontrol.fail_audit()"
        )
    try:
        values = CompanyWrite(
            name="Uncommitted", slug="company-two", chain_url="https://two.iiko.it"
        ).model_dump(mode="json")
        with pytest.raises(psycopg.errors.RaiseException):
            repo.save(
                values,
                actor,
                second["id"],
                second["version"],
                credentials={"chain": {"login": "changed", "password": "changed-secret"}},
            )
        assert repo.get(second["id"])["version"] == second["version"]
        assert repo.check_inputs(second["id"], "chain", second["version"])[1:] == (
            "api",
            "secret-two",
        )
    finally:
        with psycopg.connect(database) as db:
            db.execute("DROP TRIGGER fail_audit ON restcontrol.events")
            db.execute("DROP FUNCTION restcontrol.fail_audit()")


def test_wrong_role_or_key_fails_closed(imported, database, tmp_path):
    repo, legacy, *_ = imported
    with pytest.raises(ValueError, match="restricted"):
        PostgresRepository(database, legacy.path.parent).validate_ready()
    with pytest.raises(ValueError, match="credentials.key"):
        PostgresRepository(repo.dsn, tmp_path / "missing")
    with pytest.raises(Problem) as error:
        repo.get("not-an-id")
    assert error.value.status == 404
    with psycopg.connect(database) as db:
        db.execute("GRANT USAGE ON SCHEMA chaika TO restcontrol_backend")
        db.execute("GRANT SELECT ON chaika.web_users TO restcontrol_backend")
    try:
        with pytest.raises(ValueError, match="another product"):
            repo.validate_ready()
    finally:
        with psycopg.connect(database) as db:
            db.execute("REVOKE ALL ON chaika.web_users FROM restcontrol_backend")
            db.execute("REVOKE ALL ON SCHEMA chaika FROM restcontrol_backend")


def test_postgres_snapshot_restore_and_private_files(imported, database, tmp_path):
    from app.saas_admin.postgres_backup import read_snapshot, restore_postgres, snapshot_postgres

    repo, legacy, _, first, _, _ = imported
    snapshot = tmp_path / "pg-snapshot"
    manifest = snapshot_postgres(repo.dsn, legacy.path.parent, snapshot)
    data, checked, key = read_snapshot(snapshot)
    assert checked == manifest
    assert data["sessions"] == data["tenant_sessions"] == []
    assert manifest["counts"]["companies"] == 2
    assert not (snapshot / "registry.sqlite3").exists()
    assert snapshot.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in snapshot.iterdir())
    with pytest.raises(FileExistsError):
        snapshot_postgres(repo.dsn, legacy.path.parent, snapshot)
    with pytest.raises(ValueError, match="operator"):
        restore_postgres(snapshot, repo.dsn, tmp_path / "runtime-restore")
    assert not (tmp_path / "runtime-restore").exists()
    with pytest.raises(ValueError, match="empty"):
        restore_postgres(snapshot, database, tmp_path / "populated-restore")
    assert not (tmp_path / "populated-restore").exists()
    with psycopg.connect(database, autocommit=True) as db:
        db.execute("TRUNCATE restcontrol.companies,restcontrol.platform_memberships,"
                   "restcontrol.attempts,restcontrol.check_targets,restcontrol.imports CASCADE")
    restored = tmp_path / "pg-restored"
    restore_postgres(snapshot, database, restored)
    copy = PostgresRepository(repo.dsn, restored)
    assert copy.get(first["id"]) == repo.get(first["id"])
    current_version = copy.get(first["id"])["version"]
    assert copy.check_inputs(first["id"], "chain", current_version)[2] == "secret-one"
    assert (restored / "credentials.key").read_bytes() == key
    with copy.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM tenant_sessions").fetchone()["n"] == 0
    (snapshot / "registry.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        read_snapshot(snapshot)


def test_postgres_restore_missing_auth_identity_rolls_back(imported, database, tmp_path):
    from app.saas_admin.postgres_backup import restore_postgres, snapshot_postgres

    repo, legacy, *_ = imported
    snapshot = tmp_path / "pg-snapshot"
    snapshot_postgres(repo.dsn, legacy.path.parent, snapshot)
    with psycopg.connect(database, autocommit=True) as db:
        db.execute("TRUNCATE restcontrol.companies,restcontrol.platform_memberships,"
                   "restcontrol.attempts,restcontrol.check_targets,restcontrol.imports CASCADE")
        db.execute("DELETE FROM auth.users WHERE id=%s", (OWNER_AUTH,))
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            restore_postgres(snapshot, database, tmp_path / "failed-restore")
        with psycopg.connect(database) as db:
            assert db.execute("SELECT count(*) FROM restcontrol.companies").fetchone()[0] == 0
        assert not (tmp_path / "failed-restore").exists()
    finally:
        with psycopg.connect(database, autocommit=True) as db:
            db.execute("INSERT INTO auth.users VALUES(%s,'1@chaika.team','{}')", (OWNER_AUTH,))
