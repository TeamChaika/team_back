"""Operator rendering must not hand global secrets to company processes."""

from uuid import uuid4

import pytest

from app.saas_admin.runtime_operator import RuntimeOperator, private_json


def test_render_uses_saved_own_credentials_and_scoped_roles(tmp_path, monkeypatch):
    company_id = uuid4()
    rms_id = uuid4()
    company = {
        "id": str(company_id),
        "version": 3,
        "name": "Company",
        "domain": "customer.example.org",
        "status": "active",
        "subscription": {"timezone": "Asia/Tokyo"},
        "chain_url": "https://own-chain.example/resto",
        "rms": [
            {
                "id": str(rms_id),
                "name": "Own RMS",
                "enabled": True,
                "url": "https://own-rms.example",
            }
        ],
    }

    class Repo:
        def get(self, identifier):
            assert identifier == str(company_id)
            return company

        def tenant_actor_session(self, token, company_id):
            from app.saas_admin.repository import Problem

            raise Problem(401, "unauthorized", "Synthetic invalid session")

        def check_inputs(self, identifier, connection, version):
            assert identifier == str(company_id) and version == 3
            return (
                ("https://own-chain.example/resto", "own-chain-login", "own-chain-secret")
                if connection == "chain"
                else ("https://own-rms.example", "own-rms-login", "own-rms-secret")
            )

    common = "host=127.0.0.1 port=55483 dbname=tenant_test"
    config = {
        "process_isolation": {"mode": "local-test"},
        "company_id": str(company_id),
        "runtime_root": str(tmp_path),
        "operator_dsn": common + " user=operator password=operator-secret",
        "registry_dsn": common + " user=registry password=registry-secret",
        "runtime_dsn": common + f" user=c_{company_id.hex}_runtime password=tenant-secret",
        "payments_dsn": common
        + f" user=c_{company_id.hex}_payments_runtime password=payment-secret",
        "auth_url": "https://auth.example",
        "auth_anon_key": "anon",
        "auth_admin_key": "global-auth-secret",
        "verifier_socket": str(tmp_path / "verifier.sock"),
    }

    class Settings:
        def runtime_settings(self, identifier, *, expected_version):
            assert identifier == str(company_id) and expected_version == 3
            return {
                "document_settings": {
                    "commercial_seller_json": {"name": "Central own seller"},
                    "bot_username": "own_bot",
                    "bot_token": "own-bot-secret",
                },
                "assistant_settings": {
                    "provider": "openai",
                    "api_key": "central-own-ai",
                    "model": "own-model",
                },
            }

    monkeypatch.setattr(
        "app.saas_admin.company_module_settings.CompanyModuleSettings", lambda repo: Settings()
    )
    operator = RuntimeOperator(config, Repo())
    # Actual standalone factory must provide Auth for owner acceptance verification.
    import app.saas_admin.runtime_operator as operator_module

    config["registry_data_directory"] = str(tmp_path / "registry")
    monkeypatch.setattr(operator_module, "PostgresRepository", lambda *args: Repo())
    standalone = RuntimeOperator(config)
    from app.saas_admin.supabase_auth import SupabaseAuthClient

    assert isinstance(standalone.repo.auth, SupabaseAuthClient)
    assert standalone._owns_auth is True
    standalone.close()
    env = operator.prepare_manifest()
    assert private_json(operator.manifest) == env
    assert operator.manifest.stat().st_mode & 0o077 == 0
    assert env["RESTCONTROL_TENANT_TIMEZONE"] == "Asia/Tokyo"
    assert env["RESTCONTROL_TENANT_IIKO_BASE_URL"] == "https://own-chain.example/resto/api"
    assert env["RESTCONTROL_TENANT_IIKO_PASSWORD"] == "own-chain-secret"
    assert env["RESTCONTROL_TENANT_AI_API_KEY"] == "central-own-ai"
    assert env["RESTCONTROL_TENANT_DOCUMENTS_BOT_TOKEN"] == "own-bot-secret"

    text = operator.manifest.read_text()
    assert all(
        secret not in text
        for secret in ["operator-secret", "registry-secret", "global-auth-secret"]
    )
    from app.saas_admin.runtime_operator import atomic_private_json

    stale_env = {**env, "RESTCONTROL_TENANT_CONFIGURATION_VERSION": "2"}
    atomic_private_json(operator.manifest, stale_env)
    with pytest.raises(ValueError, match="Child manifest"):
        operator.child_environment("portal")
    atomic_private_json(operator.manifest, env)
    collector = operator.child_environment("collector")
    assert not any(
        key.startswith(("RESTCONTROL_TENANT_PAYMENTS_", "RESTCONTROL_TENANT_VERIFIER_"))
        for key in collector
    )
    assert (
        operator.child_environment("portal")["RESTCONTROL_TENANT_PAYMENTS_DATABASE_URL"]
        == config["payments_dsn"]
    )
    with pytest.raises(ValueError, match="same explicitly configured database"):
        RuntimeOperator(
            {
                **config,
                "payments_dsn": config["payments_dsn"].replace(
                    "dbname=tenant_test", "dbname=foreign"
                ),
            },
            Repo(),
        )
    with pytest.raises(ValueError, match="explicit restricted role"):
        RuntimeOperator(
            {**config, "payments_dsn": common + " user=operator"}, Repo()
        ).prepare_manifest()

    # Exercise the actual documented operator verifier factory, not an injected app.
    from fastapi.testclient import TestClient

    key_file = operator.runtime.runtime_path("verifier.key")
    config["verifier_grants"] = [
        {"company_id": str(company_id), "role": "portal", "secret_file": str(key_file)}
    ]
    config["identity_dsn"] = (
        common + f" user=c_{company_id.hex}_identity_runtime password=identity-secret"
    )
    app = operator.verifier_app()
    try:
        with TestClient(app) as client:
            result = client.post(
                f"/verify/{company_id}/password",
                headers={"authorization": "Bearer " + key_file.read_text()},
                json={"token": "invalid"},
            )
            assert result.status_code == 401  # Without CompanyAccounts wiring this is 503.
        assert operator.company_accounts.targets[str(company_id)].dsn == config["identity_dsn"]
        assert "identity-secret" not in operator.manifest.read_text()
    finally:
        operator.repo.auth.close()

    # Supervisor commands use explicit tenant-only settings and distinct worker capability.
    worker_key = operator.runtime.runtime_path("documents-worker.key")
    worker_key.write_text("worker-secret-" * 4)
    worker_key.chmod(0o600)
    config["verifier_grants"].append(
        {"company_id": str(company_id), "role": "documents-worker", "secret_file": str(worker_key)}
    )
    config["document_settings"] = {
        "worker_enabled": True,
        "commercial_enabled": True,
        "commercial_seller_json": {"name": "Own seller"},
    }
    config["collector_settings"] = {
        "sync_enabled": True,
        "live_sales_enabled": True,
        "sync_api_key": "own-sync-secret",
    }
    config["assistant_settings"] = {"api_key": "own-ai-secret"}
    operator.prepare_manifest()
    command, worker_env = operator.foreground_command("documents-worker")
    assert command[-1] == "app.documents.worker"
    assert worker_env["RESTCONTROL_TENANT_VERIFIER_SECRET_FILE"] == str(worker_key)
    assert not any(
        key.startswith(("RESTCONTROL_TENANT_AI_", "RESTCONTROL_TENANT_PAYMENTS_", "CHAIKA_"))
        for key in worker_env
    )
    command, scheduler_env = operator.foreground_command("scheduler")
    assert command[-2:] == ["app.scheduler", "--worker"]
    assert scheduler_env["RESTCONTROL_TENANT_SYNC_API_KEY"] == "own-sync-secret"
    assert not any(
        key.startswith(("RESTCONTROL_TENANT_DOCUMENTS_", "RESTCONTROL_TENANT_VERIFIER_"))
        for key in scheduler_env
    )
    config["assistant_settings"] = {"global_auth_key": "never"}
    with pytest.raises(ValueError, match="Unknown tenant settings"):
        operator.prepare_manifest()
    config.pop("assistant_settings")

    # A live socket with an old version is not permission to reuse stale credentials.
    socket = operator.runtime.runtime_path("collector.sock")
    socket.touch()
    import httpx

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, path, **_):
            assert path == "/_runtime/health"
            return httpx.Response(
                200, json={"company_id": str(company_id), "configuration_version": 2}
            )

    monkeypatch.setattr(httpx, "Client", lambda **_: Client())
    with pytest.raises(ValueError, match="configured runtime"):
        operator.launch("collector")


def test_public_readiness_cannot_be_supplied_by_operator_evidence(tmp_path):
    # An operator cannot pass a synthetic JSON "ready":true through config.
    from app.saas_admin.provisioning import Provisioner

    with pytest.raises(ValueError, match="All required real provisioning adapters"):
        Provisioner(object(), {"modules": lambda *_: {"ok": True, "evidence": "pretend"}})
