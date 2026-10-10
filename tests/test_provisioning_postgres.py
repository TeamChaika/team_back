"""Durable resume and readiness on an explicitly selected disposable local cluster."""

import os
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.saas_admin.initial_sync_plan import REQUIRED_JOBS, initial_sync_plan
from app.saas_admin.provisioning import STEPS, Provisioner
from app.saas_admin.runtime_registry import RuntimeRegistry
from app.tenancy.migrations import migration_fingerprint


def history_evidence():
    return {
        **initial_sync_plan(date(2026, 10, 8), date(2026, 10, 8)),
        "completed_jobs": list(REQUIRED_JOBS),
    }


@pytest.fixture
def control():
    dsn = os.environ.get("RESTCONTROL_MIGRATIONS_TEST_DSN")
    if not dsn:
        pytest.skip("Explicit disposable PostgreSQL DSN required")
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    if info.get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("Only loopback test PostgreSQL permitted")
    database = "runtime_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
        )

    @contextmanager
    def connect(write=False):
        with psycopg.connect(dsn, dbname=database, row_factory=dict_row) as db:
            db.execute("SET LOCAL search_path TO restcontrol,pg_catalog")
            yield db

    class Repo:
        pass

    repo = Repo()
    repo.connect = connect
    with connect() as db:
        db.execute(
            "CREATE SCHEMA restcontrol; CREATE TABLE "
            "restcontrol.runtime_provisioning(company_id uuid PRIMARY "
            "KEY,configuration_version bigint NOT NULL,socket_path text NOT "
            "NULL,active_socket_path text,active_version bigint,state text DEFAULT 'p"
            "ending',step text,checks jsonb "
            "DEFAULT '{}'::jsonb,attempts int DEFAULT 0,error_code "
            "text,updated_at timestamptz DEFAULT now())"
        )
    try:
        yield repo
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def test_resume_durable_only_and_version_invalidates(control):
    company = {"id": str(uuid4()), "version": 1, "status": "active"}
    calls = []
    failing = [True]

    def adapter(step):
        def check(company_id, version):
            calls.append(step)
            if step == "connections" and failing[0]:
                raise RuntimeError("sensitive details must not persist")
            return {
                "ok": True,
                "evidence": (
                    {"manifest_fingerprint": migration_fingerprint()}
                    if step == "migrations"
                    else history_evidence()
                    if step == "initial_sync"
                    else "synthetic test evidence"
                ),
            }

        return check

    adapters = {step: adapter(step) for step in STEPS}
    provision = Provisioner(control, adapters)
    registry = RuntimeRegistry(control)
    provision.enqueue(company, f"/tmp/c_{company['id'].replace('-', '')}/portal.sock")
    assert registry.resolve(company) is None
    assert provision.run(company["id"]) is False
    with control.connect() as db:
        row = db.execute("SELECT * FROM runtime_provisioning").fetchone()
        assert (
            row["state"] == "failed"
            and row["step"] == "connections"
            and row["error_code"] == "check_failed"
        )
        assert set(row["checks"]) == set(STEPS[:3])
    failing[0] = False
    # A new worker instance resumes committed successful stages.
    assert Provisioner(control, adapters).run(company["id"]) is True
    assert calls.count("migrations") == 1
    assert calls.count("connections") == 2
    assert registry.resolve(company).company_id == company["id"]
    assert registry.resolve_existing(company).company_id == company["id"]
    company["version"] = 2
    assert registry.resolve(company) is None
    provision.enqueue(company, f"/tmp/c_{company['id'].replace('-', '')}/portal.sock")
    assert registry.resolve(company) is None

    assert registry.resolve_existing(company).configuration_version == 1
    company["status"] = "suspended"
    assert registry.resolve_existing(company) is None


def test_operator_consumes_only_configured_queue_and_reports_render_failure(control):
    from types import SimpleNamespace

    from app.saas_admin.runtime_operator import RuntimeOperator

    own, foreign = uuid4(), uuid4()
    with control.connect(True) as db:
        for identifier in (own, foreign):
            db.execute(
                "INSERT INTO runtime_provisioning(company_id,configuration_version,socket_path) "
                "VALUES (%s,1,'/private/portal.sock')",
                (identifier,),
            )
    operator = RuntimeOperator.__new__(RuntimeOperator)
    operator.repo = control
    operator.runtime = SimpleNamespace(company_id=own, configuration_version=1)

    def broken_render():
        raise ValueError("secret must never become status detail")

    operator.prepare_manifest = broken_render
    assert operator.poll_once() is True
    with control.connect() as db:
        rows = {row["company_id"]: row for row in db.execute("SELECT * FROM runtime_provisioning")}
    assert rows[own]["state"] == "failed"
    assert rows[own]["error_code"] == "configuration_failed"
    assert rows[foreign]["state"] == "pending"
    assert operator.poll_once() is False
    with control.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET state='pending' WHERE company_id=%s", (own,))
    calls = []
    operator.prepare_manifest = lambda: calls.append("render")
    operator.provisioner = lambda: SimpleNamespace(run=lambda identifier: calls.append(identifier))
    assert operator.poll_once() is True
    assert calls == ["render", str(own)]


def test_missing_configuration_is_durable_and_can_resume(control):
    from app.saas_admin.provisioning import PendingCheck

    company = {"id": str(uuid4()), "version": 1, "status": "active"}
    missing = [True]

    def check(company_id, version):
        if missing[0]:
            raise PendingCheck(
                "acceptance_session_required", {"required": ["acceptance_session_file"]}
            )
        return {"ok": True, "evidence": "actual adapter completed"}

    job = Provisioner(control, {step: check for step in STEPS})
    job.enqueue(company, f"/private/c_{company['id'].replace('-', '')}/portal.sock")
    assert job.run(company["id"]) is False
    with control.connect() as db:
        row = db.execute(
            "SELECT * FROM runtime_provisioning WHERE company_id=%s", (company["id"],)
        ).fetchone()
    assert row["error_code"] == "acceptance_session_required"
    assert row["checks"]["migrations"]["ok"] is False
    missing[0] = False
    assert job.run(company["id"]) is True


def test_setup_requires_current_durable_checks_and_own_live_health(control, monkeypatch):
    import httpx
    from psycopg.types.json import Jsonb

    from app.saas_admin.runtime_registry import SETUP_CHECKS

    company = {"id": str(uuid4()), "domain": "tenant.example.org", "version": 1, "status": "active"}
    registry = RuntimeRegistry(control)
    provision = Provisioner(control, {step: lambda *_: None for step in STEPS})
    provision.enqueue(company, f"/tmp/c_{company['id'].replace('-', '')}/portal.sock")
    assert registry.resolve_setup(company) is None
    checks = {key: {"ok": True, "evidence": "test prerequisite"} for key in SETUP_CHECKS}
    checks["initial_sync"]["evidence"] = history_evidence()
    with control.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET checks=%s", (Jsonb(checks),))
    health = {"status": "ok", "company_id": company["id"], "configuration_version": 1}

    def response(request):
        assert request.headers["host"] == "api.tenant.example.org"
        assert request.url.path == "/_runtime/health"
        return httpx.Response(200, json=health)

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(response))
    assert registry.resolve_setup(company).company_id == company["id"]
    assert registry.resolve(company) is None
    assert registry.resolve_existing(company) is None
    health["company_id"] = str(uuid4())
    assert registry.resolve_setup(company) is None
    health["company_id"] = company["id"]
    health["configuration_version"] = 2
    assert registry.resolve_setup(company) is None
    health["configuration_version"] = 1
    assert registry.resolve_setup({**company, "version": 2}) is None
    assert registry.resolve_setup({**company, "status": "suspended"}) is None
    assert registry.resolve_setup({**company, "archived_at": "2026-01-01"}) is None
    checks["initial_sync"]["ok"] = False
    with control.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET checks=%s", (Jsonb(checks),))
    assert registry.resolve_setup(company) is None


def test_independent_checks_continue_and_working_is_not_full_ready(control):
    from app.saas_admin.provisioning import PendingCheck

    company = {"id": str(uuid4()), "version": 1, "status": "active"}
    calls = []

    def adapter(step):
        def check(*_):
            calls.append(step)
            if step in {"modules", "payments"}:
                raise PendingCheck(
                    "configuration_missing",
                    {"company_id": company["id"], "configuration_version": 1},
                )
            return {
                "ok": True,
                "evidence": history_evidence()
                if step == "initial_sync"
                else "synthetic foundation proof",
            }

        return check

    job = Provisioner(control, {step: adapter(step) for step in STEPS})
    registry = RuntimeRegistry(control)
    job.enqueue(company, f"/tmp/c_{company['id'].replace('-', '')}/portal.sock")
    assert job.run(company["id"]) is False
    assert calls == list(STEPS)
    assert registry.resolve(company) is None
    assert registry.resolve_existing(company) is None
    assert registry.resolve_working(company).configuration_version == 1
    assert registry.working_status(company)["working_dashboard_available"] is True
    assert registry.resolve_working({**company, "version": 2}) is None
    assert registry.resolve_working({**company, "status": "suspended"}) is None
    with control.connect() as db:
        row = db.execute("SELECT * FROM runtime_provisioning").fetchone()
    assert row["state"] == "failed" and row["step"] == "modules"
    assert row["checks"]["modules"]["ok"] is False
    # Unexpected recheck failure must discard earlier partial positive probes.
    job.adapters["modules"] = lambda *_: (_ for _ in ()).throw(RuntimeError("private"))
    assert job.run(company["id"]) is False
    with control.connect() as db:
        assert (
            "modules"
            not in db.execute("SELECT checks FROM runtime_provisioning").fetchone()["checks"]
        )


def test_payment_mutation_lock_revokes_proof_and_blocks_provisioner(control):
    from app.saas_admin.repository import Problem

    company = {"id": str(uuid4()), "version": 1, "status": "active"}
    adapters = {
        step: (
            lambda *_, step=step: {
                "ok": True,
                "evidence": history_evidence() if step == "initial_sync" else "synthetic",
            }
        )
        for step in STEPS
    }
    job, registry = Provisioner(control, adapters), RuntimeRegistry(control)
    job.enqueue(company, f"/tmp/c_{company['id'].replace('-', '')}/portal.sock")
    assert job.run(company["id"]) is True
    with registry.payment_configuration_change(company):
        assert registry.resolve(company) is None
        assert job.run(company["id"]) is False
        with pytest.raises(Problem) as busy:
            with registry.payment_configuration_change(company):
                pass
        assert busy.value.status == 503
    with control.connect() as db:
        row = db.execute("SELECT * FROM runtime_provisioning").fetchone()
    assert "payments" not in row["checks"] and row["state"] == "failed"
    assert registry.resolve_working(company) is not None
    assert job.run(company["id"]) is True
    with pytest.raises(Problem) as stale:
        with registry.payment_configuration_change({**company, "version": 2}):
            pass
    assert stale.value.status == 409
    assert registry.resolve(company) is not None


def test_legacy_module_and_payment_evidence_is_rechecked_without_resync(control):
    company = {"id": str(uuid4()), "version": 1, "status": "active"}
    calls = []

    def check(step):
        def run(*_):
            calls.append(step)
            evidence = (
                {"manifest_fingerprint": migration_fingerprint()}
                if step == "migrations"
                else {"company_id": company["id"], "configuration_version": 1, "services": {}}
                if step == "modules"
                else {"enabled": False}
                if step == "payments"
                else history_evidence()
                if step == "initial_sync"
                else "synthetic"
            )
            return {"ok": True, "evidence": evidence}

        return run

    job = Provisioner(control, {step: check(step) for step in STEPS})
    job.enqueue(company, f"/tmp/c_{company['id'].replace('-', '')}/portal.sock")
    assert job.run(company["id"])
    with control.connect(True) as db:
        db.execute(
            "UPDATE runtime_provisioning SET checks=jsonb_set(jsonb_set(checks,"
            "'{modules,evidence}','{\"probes\":[]}'::jsonb),'{payments,evidence}',"
            '\'{"terminals":[{"terminal_id":"old"}]}\'::jsonb)'
        )
    calls.clear()
    assert job.run(company["id"])
    assert calls == ["modules", "payments"]
    calls.clear()
    assert job.run(company["id"])
    assert calls == []


def test_operator_constructs_from_actual_legacy_registry_row_without_rewrite(control, tmp_path):
    from psycopg.types.json import Jsonb

    from app.saas_admin.postgres_repository import PostgresRepository
    from app.saas_admin.runtime_operator import RuntimeOperator

    company_id = uuid4()
    # Exact legacy production shape: subscription has no policy/timezone fields.
    legacy = {
        "id": str(company_id),
        "version": 8,
        "domain": "own.example.org",
        "name": "Legacy",
        "slug": "legacy",
        "status": "active",
        "archived_at": None,
        "chain_url": None,
        "rms": [],
        "subscription": {"plan": "Legacy plan", "start_date": "2026-01-01", "end_date": None},
    }
    with control.connect(True) as db:
        db.execute("CREATE TABLE companies(id uuid,archived_at timestamptz,body jsonb)")
        db.execute(
            "CREATE TABLE connections(company_id uuid,connection_id text,url text,check_json jsonb)"
        )
        db.execute("INSERT INTO companies VALUES(%s,NULL,%s)", (company_id, Jsonb(legacy)))
    repo = PostgresRepository.__new__(PostgresRepository)
    repo.connect = control.connect
    config = {
        key: "host=127.0.0.1 dbname=test"
        for key in ("operator_dsn", "registry_dsn", "runtime_dsn", "payments_dsn")
    }
    config.update(company_id=str(company_id), runtime_root=str(tmp_path))
    operator = RuntimeOperator(config, repo)
    assert operator.runtime.timezone == "Europe/Simferopol"
    assert operator.runtime.configuration_version == 8
    assert not operator.manifest.exists()
    with control.connect() as db:
        assert db.execute("SELECT body FROM companies").fetchone()["body"] == legacy


@pytest.mark.parametrize("change", ["legacy", "append", "checksum"])
def test_migration_cache_revalidates_manifest_at_same_company_version(control, monkeypatch, change):
    from app.tenancy import migrations

    company = {"id": str(uuid4()), "version": 8, "status": "active"}
    calls = []

    def check(step):
        def run(*_):
            calls.append(step)
            evidence = {"checked": True}
            if step == "migrations":
                evidence = {"manifest_fingerprint": migration_fingerprint()}
            elif step == "modules":
                evidence = {"company_id": company["id"], "configuration_version": 8, "services": {}}
            elif step == "payments":
                evidence = {"enabled": False}
            elif step == "initial_sync":
                evidence = history_evidence()
            return {"ok": True, "evidence": evidence}

        return run

    job = Provisioner(control, {step: check(step) for step in STEPS})
    socket = f"/tmp/c_{company['id'].replace('-', '')}/portal.sock"
    job.enqueue(company, socket)
    assert job.run(company["id"])
    previous = migration_fingerprint()
    if change == "legacy":
        with control.connect(True) as db:
            db.execute(
                "UPDATE runtime_provisioning SET checks=checks #- "
                "'{migrations,evidence,manifest_fingerprint}'"
            )
    else:
        manifest = migrations.load_migrations()
        if change == "append":
            changed = manifest + (migrations.Migration("future.sql", "analytics", "SELECT 1;"),)
        else:
            item = manifest[-1]
            changed = manifest[:-1] + (
                migrations.Migration(item.name, item.area, item.template + "\n-- changed"),
            )
        monkeypatch.setattr(migrations, "load_migrations", lambda: changed)
        assert migration_fingerprint() != previous
    calls.clear()
    job.enqueue(company, socket)
    assert job.run(company["id"])
    assert calls == ["migrations"]
    with control.connect() as db:
        row = db.execute("SELECT configuration_version,checks FROM runtime_provisioning").fetchone()
        assert row["configuration_version"] == 8
        assert (
            row["checks"]["migrations"]["evidence"]["manifest_fingerprint"]
            == migration_fingerprint()
        )
    calls.clear()
    assert job.run(company["id"])
    assert calls == []


@pytest.mark.parametrize("legacy", [True, False])
def test_initial_history_plan_upgrade_reruns_old_proof_only_at_same_company_version(
    control, legacy
):
    from psycopg.types.json import Jsonb

    company = {"id": str(uuid4()), "version": 8, "status": "active"}
    calls = []

    def adapter(step):
        def run(*_):
            calls.append(step)
            proof = {"checked": True}
            if step == "migrations":
                proof = {"manifest_fingerprint": migration_fingerprint()}
            elif step == "initial_sync":
                proof = history_evidence()
            elif step == "modules":
                proof = {"company_id": company["id"], "configuration_version": 8, "services": {}}
            elif step == "payments":
                proof = {"enabled": False}
            return {"ok": True, "evidence": proof}

        return run

    job = Provisioner(control, {step: adapter(step) for step in STEPS})
    socket = f"/tmp/c_{company['id'].replace('-', '')}/portal.sock"
    job.enqueue(company, socket)
    assert job.run(company["id"])
    registry = RuntimeRegistry(control)
    if legacy:
        proof = {
            "ok": True,
            "evidence": {
                "history_from": "2026-10-08",
                "history_to": "2026-10-08",
                "completed_jobs": list(REQUIRED_JOBS[:-1]),
            },
        }
        with control.connect(True) as db:
            db.execute(
                "UPDATE runtime_provisioning SET checks=jsonb_set(checks,'{initial_sync}',%s)",
                (Jsonb(proof),),
            )
        assert registry.resolve(company) is None
        assert registry.resolve_working(company) is None
    calls.clear()
    job.enqueue(company, socket)
    assert Provisioner(control, {step: adapter(step) for step in STEPS}).run(company["id"])
    assert calls == (["initial_sync"] if legacy else [])
    assert registry.resolve_working(company).configuration_version == 8
    with control.connect() as db:
        assert (
            db.execute("SELECT configuration_version FROM runtime_provisioning").fetchone()[
                "configuration_version"
            ]
            == 8
        )


@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("append_count", [1, 2])
def test_additive_migration_refresh_preserves_partial_acceptance(
    control, fail_first, append_count, monkeypatch
):
    import hashlib
    import json

    from psycopg.types.json import Jsonb

    from app.tenancy.migrations import load_migrations

    # Exercise the approved historical maintenance release, not unrelated later
    # migrations that deliberately require the ordinary provisioning checks.
    manifest = load_migrations()
    end = (
        next(
            i
            for i, item in enumerate(manifest)
            if item.name == "20261010120000_tenant_guest_links.sql"
        )
        + 1
    )
    monkeypatch.setattr("app.tenancy.migrations.load_migrations", lambda: manifest[:end])
    company = {"id": str(uuid4()), "version": 7, "status": "active"}
    entries = [(item.name, item.area, item.checksum) for item in manifest[:end]]
    previous = hashlib.sha256(json.dumps(entries[:-append_count]).encode()).hexdigest()
    modules = {
        "ok": False,
        "code": "module_configuration_required",
        "evidence": {
            "company_id": company["id"],
            "configuration_version": 7,
            "probes": [{"path": "/api/me", "ok": True}, {"path": "/api/overview", "ok": True}],
            "services": {"telegram_identity": False, "assistant_configured": False},
        },
    }
    checks = {
        "migrations": {"ok": True, "evidence": {"manifest_fingerprint": previous}},
        "modules": modules,
        "payments": {"ok": False, "evidence": {"terminals": []}},
    }
    calls = []

    def migrations(*args):
        calls.append("migrations")
        if fail_first and len(calls) == 1:
            raise RuntimeError("Transient maintenance failure")
        return {"ok": True, "evidence": {"manifest_fingerprint": migration_fingerprint()}}

    def forbidden(*args):
        pytest.fail("Migration maintenance must not rerun independent acceptance")

    provision = Provisioner(
        control, {step: migrations if step == "migrations" else forbidden for step in STEPS}
    )
    socket = f"/tmp/c_{company['id'].replace('-', '')}/portal.sock"
    provision.enqueue(company, socket)
    with control.connect(True) as db:
        db.execute(
            "UPDATE runtime_provisioning SET checks=%s,state='failed',step='modules',"
            "error_code='module_configuration_required'",
            (Jsonb(checks),),
        )
    assert provision.enqueue_migration_refresh(company, socket)
    # Simulate service restart: the intent is persisted, not held in process memory.
    restarted = Provisioner(control, provision.adapters)
    if fail_first:
        assert restarted.run(company["id"]) is False
        with control.connect() as db:
            failed = db.execute("SELECT * FROM runtime_provisioning").fetchone()
        assert failed["checks"]["modules"] == modules
        assert (
            failed["checks"]["migration_refresh"]["error_code"] == "module_configuration_required"
        )
        assert restarted.enqueue_migration_refresh(company, socket)
    assert restarted.run(company["id"])
    with control.connect() as db:
        row = db.execute("SELECT * FROM runtime_provisioning").fetchone()
    assert calls == ["migrations"] * (2 if fail_first else 1)
    assert row["state"] == "failed"
    assert row["step"] == "modules"
    assert row["error_code"] == "module_configuration_required"
    assert row["checks"]["modules"] == modules
    assert row["checks"]["payments"] == checks["payments"]
    assert "migration_refresh" not in row["checks"]
    assert (
        row["checks"]["migrations"]["evidence"]["manifest_fingerprint"] == migration_fingerprint()
    )


def test_unknown_or_edited_migration_cannot_get_maintenance_scope(control):
    from app.saas_admin.provisioning import additive_migration_refresh

    assert not additive_migration_refresh(
        {"migrations": {"ok": True, "evidence": {"manifest_fingerprint": "different-content"}}}
    )
    assert not additive_migration_refresh(
        {"migrations": {"ok": False, "evidence": {"manifest_fingerprint": migration_fingerprint()}}}
    )


def test_identity_migration_append_requires_full_acceptance():
    import hashlib
    import json

    from app.saas_admin.provisioning import additive_migration_refresh
    from app.tenancy.migrations import load_migrations

    entries = [(item.name, item.area, item.checksum) for item in load_migrations()]
    primary_index = next(
        index
        for index, item in enumerate(entries)
        if item[0] == "20261010100000_tenant_primary_admin_projection.sql"
    )
    fingerprint = hashlib.sha256(json.dumps(entries[:primary_index]).encode()).hexdigest()
    assert not additive_migration_refresh(
        {"migrations": {"ok": True, "evidence": {"manifest_fingerprint": fingerprint}}}
    )


@pytest.mark.parametrize("resume", ["run", "enqueue"])
def test_interrupted_bot_refresh_cannot_skip_acceptance_for_new_release(
    control, monkeypatch, resume
):
    from psycopg.types.json import Jsonb

    from app.saas_admin.provisioning import PendingCheck

    company = {"id": str(uuid4()), "version": 9, "status": "active"}
    old_target = migration_fingerprint()
    calls = []

    def adapter(step):
        def check(*args):
            calls.append(step)
            if step == "modules":
                raise PendingCheck("fresh_acceptance_required", {"fresh": True})
            return {
                "ok": True,
                "evidence": {"manifest_fingerprint": "new-identity-release"}
                if step == "migrations"
                else {"checked": True},
            }

        return check

    provision = Provisioner(control, {step: adapter(step) for step in STEPS})
    socket = f"/tmp/c_{company['id'].replace('-', '')}/portal.sock"
    provision.enqueue(company, socket)
    checks = {
        "migration_refresh": {
            "configuration_version": 9,
            "target_manifest_fingerprint": old_target,
            "state": "failed",
            "step": "modules",
            "error_code": "module_configuration_required",
        },
        "modules": {"ok": False, "evidence": {"old": True}},
    }
    with control.connect(True) as db:
        db.execute("UPDATE runtime_provisioning SET state='failed',checks=%s", (Jsonb(checks),))
    monkeypatch.setattr(
        "app.tenancy.migrations.migration_fingerprint", lambda: "new-identity-release"
    )
    if resume == "enqueue":
        assert provision.enqueue_migration_refresh(company, socket) is False
        provision.enqueue(company, socket)
    assert provision.run(company["id"]) is False
    assert "modules" in calls
    with control.connect() as db:
        row = db.execute("SELECT * FROM runtime_provisioning").fetchone()
    assert "migration_refresh" not in row["checks"]
    assert row["checks"]["modules"]["code"] == "fresh_acceptance_required"
    assert row["checks"]["modules"]["evidence"] == {"fresh": True}
