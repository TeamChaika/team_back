"""Durable resume and readiness on an explicitly selected disposable local cluster."""

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.saas_admin.provisioning import STEPS, Provisioner
from app.saas_admin.runtime_registry import RuntimeRegistry


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
            return {"ok": True, "evidence": "synthetic test evidence"}

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
            return {"ok": True, "evidence": "synthetic foundation proof"}

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
    adapters = {step: lambda *_: {"ok": True, "evidence": "synthetic"} for step in STEPS}
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
                {"company_id": company["id"], "configuration_version": 1, "services": {}}
                if step == "modules"
                else {"enabled": False}
                if step == "payments"
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
            "'{\"terminals\":[{\"terminal_id\":\"old\"}]}'::jsonb)"
        )
    calls.clear()
    assert job.run(company["id"])
    assert calls == ["modules", "payments"]
    calls.clear()
    assert job.run(company["id"])
    assert calls == []
