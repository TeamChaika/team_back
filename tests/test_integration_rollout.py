from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

from app.saas_admin.integration_rollout import invalidate_integrations, reconcile_integrations


class Database:
    def __init__(self, checks):
        self.checks = deepcopy(checks)
        self.updates = 0

    def execute(self, query, params):
        if "pg_try_advisory" in query:
            return SimpleNamespace(fetchone=lambda: {"locked": True})
        if query.startswith("SELECT checks"):
            return SimpleNamespace(fetchone=lambda: {"checks": deepcopy(self.checks)})
        assert query.startswith("UPDATE runtime_provisioning")
        self.checks = params[0].obj
        self.updates += 1
        return SimpleNamespace(fetchone=lambda: None)


def fixture():
    company = {"id": "11111111-1111-1111-1111-111111111111", "version": 3}
    checks = {
        "migrations": {"ok": True},
        "payments": {"ok": True},
        "modules": {
            "ok": True,
            "evidence": {
                "company_id": company["id"],
                "configuration_version": 3,
                "services": {
                    "assistant_configured": True,
                    "telegram_identity": True,
                    "scheduler": True,
                },
                "probes": [{"path": "/api/me", "ok": True}],
            },
        },
    }
    return company, Database(checks)


def operator(company, db):
    @contextmanager
    def connect(*args):
        yield db

    return SimpleNamespace(
        company=company,
        repo=SimpleNamespace(connect=connect),
        config={"document_settings": {"bot_token": "fake-bot"}},
    )


def test_scoped_invalidation_preserves_company_and_unrelated_evidence():
    company, db = fixture()
    before = deepcopy(db.checks)
    invalidate_integrations(db, company, 2, ["telegram"])
    assert company["version"] == 3
    assert db.checks["migrations"] == before["migrations"]
    assert db.checks["payments"] == before["payments"]
    services = db.checks["modules"]["evidence"]["services"]
    assert services == {"assistant_configured": True, "telegram_identity": False, "scheduler": True}
    invalidate_integrations(db, company, 3, ["assistant"])
    assert db.checks["integrations"]["changed"] == ["assistant", "telegram"]


def test_failed_or_stale_probe_never_restores_evidence(monkeypatch):
    company, db = fixture()
    invalidate_integrations(db, company, 4, ["telegram"])
    monkeypatch.setattr("app.saas_admin.integration_rollout.probe_integrations", lambda *a: None)
    reconcile_integrations(operator(company, db), 4, 123)
    assert db.checks["integrations"]["state"] == "pending"
    monkeypatch.setattr(
        "app.saas_admin.integration_rollout.probe_integrations",
        lambda *a: {"telegram_identity": True},
    )
    reconcile_integrations(operator(company, db), 3, 123)
    assert db.checks["integrations"]["state"] == "pending"
    assert db.checks["modules"]["evidence"]["services"]["telegram_identity"] is False
    reconcile_integrations(operator(company, db), 4, 123)
    assert db.checks["integrations"]["state"] == "applied"
    assert db.checks["modules"]["evidence"]["services"]["telegram_identity"] is True


def test_invalid_bot_reports_failed_but_deletion_applies(monkeypatch):
    company, db = fixture()
    invalidate_integrations(db, company, 4, ["telegram"])
    monkeypatch.setattr(
        "app.saas_admin.integration_rollout.probe_integrations",
        lambda *a: {"telegram_identity": False},
    )
    reconcile_integrations(operator(company, db), 4, 123)
    assert db.checks["integrations"]["state"] == "failed"
    invalidate_integrations(db, company, 5, ["telegram"])
    op = operator(company, db)
    op.config["document_settings"]["bot_token"] = ""
    reconcile_integrations(op, 5, 123)
    assert db.checks["integrations"]["state"] == "applied"
    assert db.checks["modules"]["evidence"]["services"]["telegram_identity"] is False


def test_assistant_proof_retained_when_only_telegram_changes():
    company, db = fixture()
    db.checks["integrations"] = {
        "revision": 2,
        "state": "applied",
        "services": {"assistant_configured": True},
    }
    invalidate_integrations(db, company, 3, ["telegram"])
    assert db.checks["integrations"]["services"] == {"assistant_configured": True}


def test_fleet_revision_reloads_only_portal_and_documents_worker(tmp_path, monkeypatch):
    from datetime import date

    from app.saas_admin.initial_sync_plan import REQUIRED_JOBS, initial_sync_plan
    from app.saas_admin.runtime_fleet import FleetSupervisor
    from app.tenancy.migrations import migration_fingerprint

    company, db = fixture()
    company["status"] = "active"
    state = {
        "configuration_version": 3,
        "state": "ready",
        "error_code": None,
        "checks": {
            "database_roles": {"ok": True},
            "migrations": {
                "ok": True,
                "evidence": {"manifest_fingerprint": migration_fingerprint()},
            },
            "initial_sync": {
                "ok": True,
                "evidence": {
                    **initial_sync_plan(date(2026, 10, 8), date(2026, 10, 8)),
                    "completed_jobs": list(REQUIRED_JOBS),
                },
            },
        },
    }

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: state))

    path = tmp_path / "own.json"
    config = {"company_id": company["id"], "document_settings": {"worker_enabled": True}}
    prepared = []
    preparer = SimpleNamespace(
        repo=SimpleNamespace(get=lambda _: company, connect=connect),
        settings_service=SimpleNamespace(integrations_revision=lambda _: 2),
        prepare=lambda _: prepared.append(True) or path,
    )
    supervisor = FleetSupervisor(preparer)
    roles = ["work", "run-portal", "run-documents-worker", "run-collector", "run-scheduler"]
    supervisor.children = {(str(path), role): object() for role in roles}
    supervisor.versions = {key: 3 for key in supervisor.children}
    supervisor.prepared_versions[str(path)] = 3
    supervisor.integrations_revisions[str(path)] = 1
    stopped = []

    def stop(key):
        stopped.append(key[1])
        supervisor.children.pop(key)

    monkeypatch.setattr(supervisor, "stop", stop)
    monkeypatch.setattr(supervisor, "start", lambda *args: None)
    monkeypatch.setattr("app.saas_admin.runtime_fleet.read_operator_json", lambda _: config)
    monkeypatch.setattr(
        "app.saas_admin.runtime_operator.RuntimeOperator",
        lambda *a: SimpleNamespace(prepare_manifest=lambda: None, close=lambda: None),
    )
    supervisor.tick_company(path)
    assert set(stopped) == {"run-portal", "run-documents-worker"}
    assert supervisor.integrations_revisions[str(path)] == 2
    assert len(prepared) == 1
    supervisor.tick_company(path)
    assert len(prepared) == 1


def test_bad_bot_checked_before_worker_health_and_ai_independent(monkeypatch):
    from pathlib import Path
    from uuid import UUID

    from app.saas_admin.integration_rollout import probe_integrations

    company, db = fixture()
    op = operator(company, db)
    op.runtime = SimpleNamespace(
        company_id=UUID(company["id"]),
        configuration_version=3,
        api_origin="https://api.test.example",
        runtime_path=lambda _: Path("/tmp/test.sock"),
    )
    responses = [
        SimpleNamespace(
            status_code=200,
            json=lambda: {
                "company_id": company["id"],
                "configuration_version": 3,
                "integrations_revision": 4,
                "status": "ok",
                "assistant_configured": True,
            },
        ),
        SimpleNamespace(status_code=401, json=lambda: {"ok": False}),
    ]

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr("app.saas_admin.integration_rollout.httpx.Client", Client)
    monkeypatch.setattr(
        "app.saas_admin.runtime_acceptance.heartbeat",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("bad bot must be rejected before heartbeat")
        ),
    )
    assert probe_integrations(op, 4, ["assistant", "telegram"], None) == {
        "assistant_configured": True,
        "telegram_identity": False,
    }


def test_partial_ai_proof_applies_while_telegram_worker_is_pending(monkeypatch):
    company, db = fixture()
    invalidate_integrations(db, company, 4, ["assistant", "telegram"])
    monkeypatch.setattr(
        "app.saas_admin.integration_rollout.probe_integrations",
        lambda *a: {"assistant_configured": True},
    )
    reconcile_integrations(operator(company, db), 4, None)
    assert db.checks["integrations"]["state"] == "pending"
    assert db.checks["modules"]["evidence"]["services"]["assistant_configured"] is True
    assert db.checks["modules"]["evidence"]["services"]["telegram_identity"] is False


def test_release_migration_requeues_previously_completed_runtime(tmp_path, monkeypatch):
    from app.saas_admin.runtime_fleet import FleetSupervisor

    company, _ = fixture()
    company["status"] = "active"
    state = {"configuration_version": 3, "state": "failed", "checks": {}, "error_code": None}

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: state))

    path = tmp_path / "own.json"
    config = {"company_id": company["id"]}
    supervisor = FleetSupervisor(
        SimpleNamespace(repo=SimpleNamespace(get=lambda _: company, connect=connect))
    )
    supervisor.prepared_versions[str(path)] = 3
    supervisor.integrations_revisions[str(path)] = 1
    calls = []
    monkeypatch.setattr(supervisor, "start", lambda *a: calls.append("start"))
    monkeypatch.setattr("app.saas_admin.runtime_fleet.read_operator_json", lambda _: config)
    monkeypatch.setattr(
        "app.saas_admin.runtime_operator.RuntimeOperator",
        lambda *a: SimpleNamespace(
            runtime=SimpleNamespace(runtime_path=lambda _: tmp_path / "portal.sock"),
            provisioner=lambda: SimpleNamespace(enqueue=lambda *args: calls.append("enqueue")),
            close=lambda: None,
        ),
    )
    supervisor.tick_company(path)
    assert calls == ["enqueue", "start"]


def test_first_bot_fresh_worker_unlocks_telegram_writes(monkeypatch):
    from pathlib import Path
    from uuid import UUID

    from app.saas_admin.feature_readiness import feature_readiness

    company, db = fixture()
    company.update(status="active", modules={"documents": True})
    db.checks["modules"]["evidence"]["services"]["documents_worker"] = False
    op = operator(company, db)
    op.config["document_settings"]["bot_username"] = "own_bot"
    op.runtime = SimpleNamespace(
        company_id=UUID(company["id"]),
        configuration_version=3,
        api_origin="https://api.test.example",
        runtime_path=lambda _: Path("/tmp/test.sock"),
    )

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, path, **kwargs):
            if path == "/_runtime/health":
                body = {
                    "company_id": company["id"],
                    "configuration_version": 3,
                    "integrations_revision": 4,
                    "status": "ok",
                }
            else:
                body = {"ok": True, "result": {"is_bot": True, "username": "own_bot"}}
            return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr("app.saas_admin.integration_rollout.httpx.Client", Client)
    invalidate_integrations(db, company, 4, ["telegram"])
    monkeypatch.setattr("app.saas_admin.runtime_acceptance.heartbeat", lambda *a: False)
    reconcile_integrations(op, 4, 123)
    assert not feature_readiness(company, db.checks, working=True)["notifications.telegram"][
        "write"
    ]
    assert db.checks["modules"]["evidence"]["services"]["documents_worker"] is False
    monkeypatch.setattr("app.saas_admin.runtime_acceptance.heartbeat", lambda *a: True)
    reconcile_integrations(op, 4, 123)
    assert db.checks["modules"]["evidence"]["services"]["documents_worker"] is True
    assert (
        feature_readiness(company, db.checks, working=True)["notifications.telegram"]["write"]
        is True
    )


def test_company_revision_race_does_not_interrupt_other_companies(tmp_path, monkeypatch):
    from app.saas_admin.repository import Problem
    from app.saas_admin.runtime_fleet import FleetSupervisor

    first, second = tmp_path / "c_first.json", tmp_path / "c_second.json"
    first.touch()
    second.touch()
    supervisor = FleetSupervisor(
        SimpleNamespace(
            root=tmp_path,
            prepare_pending=lambda **kwargs: None,
        )
    )
    processed = []

    def tick_company(path):
        processed.append(path)
        if path == first:
            raise Problem(409, "integrations_version_changed", "secret must not enter logs")

    monkeypatch.setattr(supervisor, "tick_company", tick_company)
    # Owned processes must not be closed by a revision CAS race in one company.
    child = object()
    supervisor.children = {(str(second), "run-collector"): child}
    supervisor.tick()
    assert set(processed) == {first, second}
    assert supervisor.children[(str(second), "run-collector")] is child


def test_prepare_pending_revision_race_keeps_other_company_progress(tmp_path):
    from app.saas_admin.repository import Problem
    from app.saas_admin.runtime_fleet import FleetPreparer

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *a: SimpleNamespace(
                fetchall=lambda: [
                    {"company_id": "first"},
                    {"company_id": "second"},
                ]
            )
        )

    calls = []

    def prepare(company):
        calls.append(company["id"])
        if company["id"] == "first":
            raise Problem(409, "integrations_version_changed", "secret must not enter logs")
        return tmp_path / "second.json"

    preparer = object.__new__(FleetPreparer)
    preparer.repo = SimpleNamespace(connect=connect, get=lambda identifier: {"id": identifier})
    preparer.prepare = prepare
    assert preparer.prepare_pending() == [tmp_path / "second.json"]
    assert calls == ["first", "second"]
