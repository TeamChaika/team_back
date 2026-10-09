"""Exercise the actual central startup factory without external side effects."""

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.deployment import create_deployment_app
from app.saas_admin.repository import Problem
from app.saas_admin.server import BASE
from app.saas_admin.supabase_auth import SupabaseAuthClient
from app.saas_admin.vault import Vault


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.saas_admin.company_module_settings.CompanyModuleSettings",
        lambda repo: SimpleNamespace(
            runtime_settings=lambda identifier, expected_version: {
                "document_settings": {
                    "commercial_seller_json": "",
                    "bot_token": "",
                    "bot_username": "",
                },
                "assistant_settings": {"provider": "openai", "api_key": "", "model": "own-model"},
            }
        ),
    )
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    Vault(data)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "saas-admin.html").write_text("<html>registry</html>")
    company_id = uuid4()
    company = {
        "id": str(company_id),
        "name": "Own company",
        "slug": "own",
        "domain": "tenant.example.org",
        "version": 1,
        "status": "active",
        "subscription": {"timezone": "UTC"},
    }

    class Repo:
        auth = None
        reads = 0
        validated = False

        def get(self, identifier):
            assert identifier == company["id"]
            return dict(company)

        def validate_ready(self):
            self.validated = True

        def company_for_domain(self, host):
            return company if host == company["domain"] else None

        def session(self, token):
            if token != "owner-token":
                raise Problem(401, "unauthorized", "Unauthorized")
            return {"user": {"id": "owner"}, "csrf_token": "csrf"}

        @staticmethod
        def _require_owner(db, actor):
            pass

        @contextmanager
        def connect(self, write=False):
            assert not write, "Factory and status must never write registry state"
            yield self

        def execute(self, query, parameters=None):
            assert query.startswith("SELECT ")
            self.reads += 1
            return self

        def fetchone(self):
            return None

    repo = Repo()
    monkeypatch.setattr("app.saas_admin.runtime_operator.PostgresRepository", lambda *_: repo)

    class TrackedAuth(SupabaseAuthClient):
        closes = 0

        def close(self):
            self.closes += 1

    monkeypatch.setattr("app.saas_admin.supabase_auth.SupabaseAuthClient", TrackedAuth)

    # Any accidental startup/provisioning action fails immediately.
    def forbidden(*_):
        pytest.fail("Central factory must not provision or launch processes")

    for action in ("prepare_manifest", "launch", "migrations", "poll_once", "provisioner"):
        monkeypatch.setattr("app.saas_admin.runtime_operator.RuntimeOperator." + action, forbidden)
    capability = tmp_path / "verifier.key"
    capability.write_text("private-test-capability-long-enough-for-grant")
    capability.chmod(0o600)
    database = "host=127.0.0.1 port=55483 dbname=tenant_test"
    config = {
        "company_id": str(company_id),
        "registry_data_directory": str(data),
        "runtime_root": str(tmp_path / "tenants"),
        "operator_dsn": database + " user=operator",
        "registry_dsn": database + " user=restcontrol_backend",
        "runtime_dsn": database + f" user=c_{company_id.hex}_runtime",
        "payments_dsn": database + f" user=c_{company_id.hex}_payments_runtime",
        "identity_dsn": database + f" user=c_{company_id.hex}_identity_runtime",
        "auth_url": "https://auth.example.org",
        "auth_anon_key": "anon",
        "auth_admin_key": "private-admin-key",
        "verifier_grants": [
            {"company_id": str(company_id), "role": "portal", "secret_file": str(capability)}
        ],
        "public_dns_targets": {
            "frontend": {"type": "CNAME", "value": "static.example.org"},
            "api": {"type": "A", "value": "8.8.8.8"},
        },
    }
    manifest = tmp_path / "operator.json"
    manifest.write_text(json.dumps(config))
    manifest.chmod(0o600)
    return data, dist, repo, config, manifest


def build(deployment):
    data, dist, _, _, manifest = deployment
    return create_deployment_app(
        data, dist, "https://rc.example.org", "production", runtime_config=manifest
    )


def test_actual_central_assembly_has_accounts_dns_and_readiness_gate(deployment):
    _, _, repo, config, _ = deployment
    app = build(deployment)
    assert repo.validated
    assert app.state.repository is repo
    assert app.state.company_accounts.repository is repo
    target = app.state.company_accounts.targets[config["company_id"]]
    assert target.dsn == config["identity_dsn"]
    assert not target.runtime.runtime_directory.exists()
    with TestClient(app, base_url="https://rc.example.org") as client:
        client.cookies.set("saas_owner_session", "owner-token")
        status = client.get(BASE + "/companies/" + config["company_id"] + "/provisioning")
        assert status.status_code == 200
        value = status.json()
        assert value["configured"] is True
        assert value["ready"] is False
        assert value["dns_records"] == [
            {"name": "tenant.example.org", "type": "CNAME", "value": "static.example.org"},
            {"name": "api.tenant.example.org", "type": "A", "value": "8.8.8.8"},
        ]
        headers = {"Host": "api.tenant.example.org", "Origin": "https://tenant.example.org"}
        context = client.get("/api/saas-context", headers=headers)
        assert context.json()["full_dashboard_ready"] is False
        portal = client.get("/api/overview", headers=headers)
        assert portal.status_code == 503
        assert portal.json()["detail"]["code"] == "runtime_not_ready"
        assert not any(route.path.startswith("/verify/") for route in app.routes)
        assert repo.auth.closes == 0
    assert repo.auth.closes == 1


@pytest.mark.parametrize(
    "case",
    [
        "public_file",
        "symlink",
        "data_mismatch",
        "missing_dns",
        "private_dns",
        "wrong_identity_role",
    ],
)
def test_bad_trusted_configuration_fails_closed(deployment, case):
    _, _, repo, config, manifest = deployment
    if case == "public_file":
        manifest.chmod(0o644)
    elif case == "symlink":
        alias = manifest.with_name("alias.json")
        alias.symlink_to(manifest)
        deployment = (*deployment[:-1], alias)
    else:
        if case == "data_mismatch":
            config["registry_data_directory"] += "-foreign"
        elif case == "missing_dns":
            del config["public_dns_targets"]["api"]
        elif case == "private_dns":
            config["public_dns_targets"]["api"]["value"] = "127.0.0.1"
        else:
            config["identity_dsn"] = config["operator_dsn"]
        manifest.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        build(deployment)
    assert repo.auth is None or repo.auth.closes == 1


def test_auth_closed_when_actual_server_factory_rejects_static_directory(deployment):
    data, _, repo, _, manifest = deployment
    with pytest.raises(ValueError, match="built static"):
        create_deployment_app(
            data, None, "https://rc.example.org", "production", runtime_config=manifest
        )
    assert repo.auth.closes == 1


def test_default_assembly_stays_limited(deployment, monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    data, dist, repo, _, _ = deployment

    def repository(*_, auth, **__):
        repo.auth = auth
        return repo

    monkeypatch.setattr("app.saas_admin.postgres_repository.PostgresRepository", repository)
    monkeypatch.setattr(
        "app.saas_admin.server.SupabaseSettings",
        lambda: SimpleNamespace(
            database_url=SecretStr("test"),
            supabase_url="https://auth.example.org",
            anon_key=SecretStr("anon"),
            auth_admin_key=SecretStr("private"),
        ),
    )
    app = create_deployment_app(data, dist, "https://rc.example.org", "production")
    assert app.state.company_accounts is None
    with TestClient(app, base_url="https://rc.example.org") as client:
        client.cookies.set("saas_owner_session", "owner-token")
        value = client.get(BASE + "/companies/" + deployment[3]["company_id"] + "/provisioning")
        assert value.json()["configured"] is False
        assert value.json()["dns_records"] == []
        headers = {"Host": "api.tenant.example.org", "Origin": "https://tenant.example.org"}
        assert "full_dashboard_ready" not in client.get("/api/saas-context", headers=headers).json()
        assert client.get("/api/overview", headers=headers).status_code == 403


def test_serve_command_uses_actual_explicit_assembly(deployment, monkeypatch):
    from app.saas_admin.__main__ import main

    data, dist, repo, _, manifest = deployment
    monkeypatch.setattr(
        "sys.argv",
        [
            "saas-admin",
            "serve",
            "--data-dir",
            str(data),
            "--dist-dir",
            str(dist),
            "--mode",
            "production",
            "--origin",
            "https://rc.example.org",
            "--runtime-config",
            str(manifest),
            "--port",
            "8299",
        ],
    )
    served = []

    def serve(app, **options):
        assert options == {
            "host": "127.0.0.1",
            "port": 8299,
            "proxy_headers": False,
            "access_log": False,
        }
        assert app.state.company_accounts.repository is repo
        with TestClient(app, base_url="https://rc.example.org") as client:
            assert client.get(BASE + "/health").status_code == 200
        served.append(app)

    monkeypatch.setattr("uvicorn.run", serve)
    main()
    assert len(served) == 1
    assert repo.auth.closes == 1


def test_fleet_factory_has_no_anchor_and_discovers_new_company(deployment, tmp_path, monkeypatch):
    from app.saas_admin.runtime_fleet import FleetPreparer

    data, dist, repo, original, _ = deployment
    monkeypatch.setattr("app.saas_admin.postgres_repository.PostgresRepository", lambda *args: repo)
    template = {
        key: value
        for key, value in original.items()
        if key
        not in {"company_id", "runtime_dsn", "payments_dsn", "identity_dsn", "verifier_grants"}
    }
    edge = tmp_path / "edge.key"
    edge.write_text("fleet-edge-capability-" * 3)
    edge.chmod(0o600)
    template.update(
        edge_token_file=str(edge),
        acceptance_root=str(tmp_path / "acceptance"),
        verifier_socket=str(tmp_path / "verifier.sock"),
    )
    config = {"operator_directory": str(tmp_path / "operators"), "operator_template": template}
    preparer = FleetPreparer(config, repo)
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    app = create_deployment_app(
        data, dist, "https://rc.example.org", "production", runtime_config=path
    )
    assert original["company_id"] not in app.state.company_accounts.targets
    preparer.prepare(repo.get(original["company_id"]))
    target = app.state.company_accounts.targets[original["company_id"]]
    assert str(target.runtime.company_id) == original["company_id"]
    with TestClient(app) as client:
        response = client.get(
            "https://api.tenant.example.org/api/saas-context",
            headers={"origin": "https://tenant.example.org"},
        )
        assert response.status_code == 200
        assert response.json()["full_dashboard_available"] is False
