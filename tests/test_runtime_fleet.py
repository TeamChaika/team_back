from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.saas_admin.runtime_fleet import FleetPreparer
from app.saas_admin.runtime_operator import private_json


@pytest.fixture(autouse=True)
def central_settings(monkeypatch):
    from types import SimpleNamespace

    def settings(identifier, *, expected_version):
        return {
            "document_settings": {
                "commercial_seller_json": "",
                "bot_token": "",
                "bot_username": "",
            },
            "assistant_settings": {"provider": "openai", "api_key": "", "model": "own-model"},
        }

    monkeypatch.setattr(
        "app.saas_admin.company_module_settings.CompanyModuleSettings",
        lambda repo: SimpleNamespace(runtime_settings=settings),
    )


def test_fleet_prepares_distinct_durable_credentials_without_authority(tmp_path):
    fleet = FleetPreparer(
        {
            "operator_directory": str(tmp_path / "control"),
            "operator_template": {
                "process_isolation": {"mode": "local-test"},
                "operator_dsn": (
                    "host=localhost port=55483 dbname=local user=ddl "
                    "password=global-secret options='-c search_path=public'"
                ),
                "runtime_root": str(tmp_path / "runtimes"),
                "registry_dsn": "unused",
                "registry_data_directory": "unused",
            },
        },
        repository=object(),
    )
    companies = [{"id": str(uuid4()), "status": "active", "version": 1} for _ in range(2)]
    manifests = [fleet.prepare(company) for company in companies]
    configs = [private_json(path) for path in manifests]
    assert fleet.prepare(companies[0]) == manifests[0]
    assert private_json(manifests[0]) == configs[0]
    passwords = set()
    for company, path, config in zip(companies, manifests, configs, strict=True):
        assert path.stat().st_mode & 0o077 == 0
        assert "acceptance_session_file" not in config
        assert "identity_targets" not in config
        for field in ("runtime_dsn", "payments_dsn", "identity_dsn"):
            value = conninfo_to_dict(config[field])
            assert value["user"].startswith("c_" + company["id"].replace("-", "") + "_")
            assert "options" not in value
            assert "global-secret" not in config[field]
            passwords.add(value["password"])
    assert len(passwords) == 6
    assert configs[0]["verifier_grants"] != configs[1]["verifier_grants"]
    before = configs[0]["runtime_dsn"]
    companies[0]["modules"] = {"documents": True, "commercial_invoices": True}
    updated = private_json(fleet.prepare(companies[0]))
    assert updated["runtime_dsn"] == before
    assert updated["document_settings"]["worker_enabled"] is True
    assert updated["document_settings"]["commercial_enabled"] is True


def test_discovery_accepts_new_binding_without_restart(tmp_path):
    from types import SimpleNamespace

    from app.saas_admin.fleet_discovery import FleetDiscovery

    company = {
        "id": str(uuid4()),
        "status": "active",
        "domain": "new.example.org",
        "version": 1,
        "subscription": {"timezone": "UTC"},
    }
    common = "host=localhost port=55483 dbname=local"
    config = {
        "operator_directory": str(tmp_path / "control"),
        "operator_template": {
            "process_isolation": {"mode": "local-test"},
            "operator_dsn": common + " user=ddl",
            "registry_dsn": common + " user=registry",
            "registry_data_directory": str(tmp_path / "registry"),
            "runtime_root": str(tmp_path / "runtime"),
            "verifier_socket": str(tmp_path / "verifier.sock"),
            "acceptance_root": str(tmp_path / "acceptance"),
        },
    }
    repo = SimpleNamespace(get=lambda identifier: company)
    preparer = FleetPreparer(config, repo)
    discovery = FleetDiscovery(config, repo)
    assert company["id"] not in discovery
    path = preparer.prepare(company)
    assert discovery[company["id"]].runtime.company_id.hex == company["id"].replace("-", "")
    grants = discovery.grants(company["id"])
    assert {grant.role for grant in grants} == {"portal", "documents-worker"}
    assert len({grant.secret for grant in grants}) == 2
    assert private_json(path)["acceptance_session_file"].endswith(".acceptance")
    bad = private_json(path)
    bad["identity_dsn"] = common + " user=ddl"
    from app.saas_admin.runtime_operator import atomic_private_json

    atomic_private_json(path, bad)
    import pytest

    with pytest.raises(ValueError, match="identity role"):
        discovery[company["id"]]


def test_supervisor_starts_only_own_roles_and_keeps_live_children(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.saas_admin.runtime_fleet import FleetSupervisor

    calls = []

    class Child:
        def poll(self):
            return None

    def spawn(command, **options):
        calls.append((command, options))
        return Child()

    supervisor = FleetSupervisor(SimpleNamespace(), popen=spawn)
    path = tmp_path / "company.json"
    assert supervisor.start(path, "work") is True
    assert supervisor.start(path, "work") is False
    # The privileged wrapper is never handed to an unprivileged child.
    identity = SimpleNamespace(
        spawn_options=lambda: {"user": 123, "group": 124, "extra_groups": []}
    )
    operator = SimpleNamespace(
        foreground_command=lambda role: (
            ["python", "-m", "app.documents.worker"],
            {"own": "value"},
        ),
        process_identity=lambda: identity,
        close=lambda: None,
    )
    supervisor.preparer.repo = object()
    monkeypatch.setattr("app.saas_admin.runtime_fleet.read_operator_json", lambda path: {})
    monkeypatch.setattr("app.saas_admin.runtime_operator.RuntimeOperator", lambda *args: operator)
    assert supervisor.start(path, "run-documents-worker") is True
    assert calls[1][0] == ["python", "-m", "app.documents.worker"]
    assert calls[1][1]["user"] == 123
    assert calls[1][1]["extra_groups"] == []
    assert len(calls) == 2
    assert calls[0][0][-3:] == ["--config", str(path), "work"]
    assert set(calls[0][1]["env"]) == {"PATH", "LANG"}


def test_supervisor_replaces_only_owned_process_on_version_change(tmp_path):
    from types import SimpleNamespace

    from app.saas_admin.runtime_fleet import FleetSupervisor

    children = []

    class Child:
        def __init__(self):
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

    def spawn(*args, **kwargs):
        child = Child()
        children.append(child)
        return child

    supervisor = FleetSupervisor(SimpleNamespace(), popen=spawn)
    path = tmp_path / "company.json"
    supervisor.start(path, "work", 1)
    assert supervisor.start(path, "work", 1) is False
    assert supervisor.start(path, "work", 2) is True
    assert children[0].stopped is True
    assert children[1].stopped is False
    supervisor.close()
    assert children[1].stopped is True


def test_supervisor_does_not_unlink_existing_live_socket(tmp_path, request):
    import shutil
    import tempfile
    from pathlib import Path

    tmp_path = Path(tempfile.mkdtemp(prefix="fleet-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path))
    import socket
    from types import SimpleNamespace

    from app.saas_admin.runtime_fleet import FleetSupervisor
    from app.saas_admin.runtime_operator import atomic_private_json

    company = uuid4()
    runtime = tmp_path / ("c_" + company.hex)
    runtime.mkdir()
    path = tmp_path / "config.json"
    atomic_private_json(
        path,
        {
            "company_id": str(company),
            "runtime_root": str(tmp_path),
            "process_isolation": {"mode": "local-test"},
        },
    )
    address = runtime / "portal.sock"
    supervisor = FleetSupervisor(SimpleNamespace(), popen=lambda *args, **kwargs: None)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(address))
        server.listen()
        assert supervisor.start(path, "run-portal", 1) is False
        assert address.exists()

        class Dead:
            def poll(self):
                return 1

            def wait(self, timeout=None):
                return 1

        key = (str(path), "run-portal")
        supervisor.children[key] = Dead()
        supervisor.stop(key)
        assert address.exists()


def test_supervisor_stops_own_process_group(monkeypatch, tmp_path):
    import signal
    from types import SimpleNamespace

    from app.saas_admin.runtime_fleet import FleetSupervisor

    groups = []
    monkeypatch.setattr(
        "app.saas_admin.runtime_fleet.os.killpg", lambda pid, sig: groups.append((pid, sig))
    )
    child = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda **kwargs: 0)
    supervisor = FleetSupervisor(SimpleNamespace(), popen=lambda *args, **kwargs: child)
    path = tmp_path / "config.json"
    supervisor.start(path, "work", 1)
    supervisor.stop((str(path), "work"))
    assert groups == [(12345, signal.SIGTERM)]


@pytest.mark.parametrize("proof", [None, "legacy", "outdated", "current"])
def test_supervisor_quiesces_own_services_before_migration_upgrade(tmp_path, monkeypatch, proof):
    from contextlib import contextmanager
    from datetime import date
    from types import SimpleNamespace

    from app.saas_admin.initial_sync_plan import REQUIRED_JOBS, initial_sync_plan
    from app.saas_admin.runtime_fleet import FleetSupervisor
    from app.tenancy.migrations import migration_fingerprint

    company = {"id": str(uuid4()), "status": "active", "version": 8}
    state = {
        "configuration_version": 8,
        "state": "pending",
        "error_code": None,
        "checks": {"database_roles": {"ok": True}, "initial_sync": {"ok": True}},
    }
    state["checks"]["initial_sync"]["evidence"] = {
        **initial_sync_plan(date(2026, 10, 8), date(2026, 10, 8)),
        "completed_jobs": list(REQUIRED_JOBS),
    }
    if proof is not None:
        evidence = {"migration_count": 11}
        if proof != "legacy":
            evidence["manifest_fingerprint"] = (
                migration_fingerprint() if proof == "current" else "old"
            )
        state["checks"]["migrations"] = {"ok": True, "evidence": evidence}

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: state))

    path = tmp_path / "own.json"
    foreign = tmp_path / "foreign.json"
    supervisor = FleetSupervisor(
        SimpleNamespace(repo=SimpleNamespace(get=lambda _: company, connect=connect))
    )
    own = (str(path), "run-portal")
    other = (str(foreign), "run-portal")
    supervisor.children = {own: object(), other: object()}
    supervisor.versions = {own: 8, other: 8}
    calls = []
    monkeypatch.setattr(supervisor, "start", lambda p, action, v: calls.append(("start", action)))

    def stop(key):
        calls.append(("stop", key[1]))
        supervisor.children.pop(key)

    monkeypatch.setattr(supervisor, "stop", stop)
    monkeypatch.setattr(
        "app.saas_admin.runtime_fleet.read_operator_json", lambda _: {"company_id": company["id"]}
    )
    supervisor.tick_company(path)
    assert other in supervisor.children
    if proof == "current":
        assert calls == [
            ("start", action) for action in ("work", "run-collector", "run-portal", "run-scheduler")
        ]
        assert own in supervisor.children
    else:
        assert calls == [("stop", "run-portal"), ("start", "work")]
        assert own not in supervisor.children


@pytest.mark.parametrize("current", [False, True])
def test_scheduler_requires_current_history_plan_and_requeues_old_ready_tenant(
    tmp_path, monkeypatch, current
):
    from contextlib import contextmanager
    from datetime import date
    from types import SimpleNamespace

    from app.saas_admin.initial_sync_plan import REQUIRED_JOBS, initial_sync_plan
    from app.saas_admin.runtime_fleet import FleetSupervisor
    from app.tenancy.migrations import migration_fingerprint

    company = {"id": str(uuid4()), "status": "active", "version": 8}
    check = {"ok": True, "evidence": {"history_from": "2026-10-08", "history_to": "2026-10-08"}}
    if current:
        check["evidence"].update(initial_sync_plan(date(2026, 10, 8), date(2026, 10, 8)))
        check["evidence"]["completed_jobs"] = list(REQUIRED_JOBS)
    state = {
        "configuration_version": 8,
        "state": "ready",
        "error_code": None,
        "checks": {
            "initial_sync": check,
            "database_roles": {"ok": True},
            "migrations": {
                "ok": True,
                "evidence": {"manifest_fingerprint": migration_fingerprint()},
            },
        },
    }

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: state))

    path, foreign = tmp_path / "own.json", tmp_path / "foreign.json"
    supervisor = FleetSupervisor(
        SimpleNamespace(repo=SimpleNamespace(get=lambda _: company, connect=connect))
    )
    own_scheduler, foreign_scheduler = (str(path), "run-scheduler"), (str(foreign), "run-scheduler")
    own_portal, own_collector = (str(path), "run-portal"), (str(path), "run-collector")
    supervisor.children = {
        key: object() for key in (own_scheduler, foreign_scheduler, own_portal, own_collector)
    }
    supervisor.versions = {key: 8 for key in supervisor.children}
    starts, stopped, enqueued = [], [], []
    monkeypatch.setattr(supervisor, "start", lambda p, action, v: starts.append(action))
    monkeypatch.setattr(
        supervisor, "stop", lambda key: stopped.append(key) or supervisor.children.pop(key)
    )
    monkeypatch.setattr(
        "app.saas_admin.runtime_fleet.read_operator_json", lambda _: {"company_id": company["id"]}
    )
    monkeypatch.setattr(
        "app.saas_admin.runtime_operator.RuntimeOperator",
        lambda *_: SimpleNamespace(
            runtime=SimpleNamespace(runtime_path=lambda name: path.parent / name),
            provisioner=lambda: SimpleNamespace(
                enqueue=lambda c, socket: enqueued.append((c["version"], socket))
            ),
        ),
    )
    supervisor.tick_company(path)
    assert foreign_scheduler in supervisor.children
    assert own_portal in supervisor.children and own_collector in supervisor.children
    if current:
        assert "run-scheduler" in starts and not stopped and not enqueued
    else:
        assert "run-scheduler" not in starts
        assert stopped == [own_scheduler]
        assert enqueued == [(8, str(path.parent / "portal.sock"))]
