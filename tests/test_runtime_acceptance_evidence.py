import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest

from app.saas_admin import runtime_acceptance
from app.saas_admin.entitlements import FEATURES
from app.saas_admin.feature_readiness import feature_readiness
from app.saas_admin.provisioning import PendingCheck


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    company_id, user_id = uuid4(), uuid4()
    company = {
        "id": str(company_id),
        "version": 5,
        "status": "active",
        "modules": {
            "analytics": True,
            "documents": True,
            "commercial_invoices": True,
            "deposits": True,
        },
    }
    path = tmp_path / "acceptance"
    path.write_text("private-session")
    path.chmod(0o600)
    operator = SimpleNamespace(
        current=lambda *_: company,
        runtime=SimpleNamespace(
            company_id=company_id,
            configuration_version=5,
            runtime_path=lambda name: tmp_path / name,
            api_origin="https://api.tenant.example",
            frontend_origin="https://tenant.example",
        ),
        repo=SimpleNamespace(
            tenant_actor_session=lambda *_: (
                SimpleNamespace(company_id=company_id, auth_user_id=user_id, kind="platform_owner"),
                {},
            )
        ),
        launch=lambda _: None,
        config={
            "acceptance_session_file": str(path),
            "history_from": "2026-10-01",
            "history_to": "2026-10-02",
            "runtime_dsn": "private-dsn",
        },
    )
    visited, failures = [], {}
    operator.acceptance_requests = []

    def respond(request):
        visited.append(request.url.path)
        operator.acceptance_requests.append(request)
        if request.url.path in failures:
            failure = failures[request.url.path]
            if failure == "network":
                raise httpx.ConnectError("sensitive connection detail", request=request)
            if failure == "invalid-json":
                return httpx.Response(
                    200, text="secret broken response", headers={"content-type": "application/json"}
                )
            return httpx.Response(failure, json={})
        if request.url.path.endswith("/export"):
            mime = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if request.url.path.startswith("/api/deposits")
                else "text/csv"
            )
            return httpx.Response(200, content=b"bytes", headers={"content-type": mime})
        if request.url.path.startswith("/bot"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "is_bot": True,
                        "username": "tenant_bot",
                    },
                },
            )
        return httpx.Response(200, json={"user": {"id": str(user_id)}, "configured": True})

    client_type = httpx.Client
    monkeypatch.setattr(
        runtime_acceptance.httpx,
        "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(respond),
            base_url=kwargs.get("base_url", "http://runtime"),
        ),
    )
    monkeypatch.setattr(runtime_acceptance, "heartbeat", lambda *_: True)
    return operator, company, visited, failures


def test_pending_check_keeps_pinned_successful_probes_and_safe_service_evidence(acceptance):
    operator, company, visited, failures = acceptance
    failures["/api/assistant/status"] = "network"
    with pytest.raises(PendingCheck) as caught:
        runtime_acceptance.check_modules(operator)
    details = caught.value.details
    assert details["company_id"] == company["id"]
    assert details["configuration_version"] == company["version"]
    assert details["probe_period"] == {"start": "2026-10-02", "end": "2026-10-02"}
    assert all(isinstance(value, bool) for value in details["services"].values())
    assert {"/api/management/accounts", "/api/payment-settings", "/api/me"} <= set(visited)
    assert "/api/assistant/status" in details["missing"]
    assert "/api/status" in visited  # A failed optional probe does not abort later probes.
    readiness = feature_readiness(
        company,
        {
            "modules": {"ok": False, "evidence": details},
            "connections": {"ok": True, "evidence": {"checked_connections": 1}},
        },
        working=True,
    )
    assert readiness["analytics.overview"]["read"] is True
    assert readiness["documents.waybills"]["write"] is True
    assert readiness["documents.approval"]["write"] is True
    assert readiness["management.settings"]["write"] is True
    assert readiness["commercial.incoming"]["write"] is False
    assert readiness["assistant.chat"]["write"] is False
    assert "sensitive" not in json.dumps(details)
    assert "private-session" not in json.dumps(details)


def test_success_requires_all_enabled_services_but_does_not_emit_secrets(acceptance):
    operator, _, _, _ = acceptance
    operator.config["document_settings"] = {
        "commercial_enabled": True,
        "commercial_submit_enabled": True,
        "commercial_counterparty_create_enabled": True,
        "worker_enabled": True,
        "commercial_seller_json": {
            "name": "Own seller",
            "inn": "1234567890",
            "bank_name": "Own bank",
            "bic": "123456789",
            "account": "1" * 20,
            "correspondent_account": "2" * 20,
        },
        "bot_token": "secret-bot-token",
        "bot_username": "tenant_bot",
    }
    operator.config["assistant_settings"] = {
        "provider": "openai",
        "api_key": "secret-ai-key",
        "model": "own-model",
    }
    result = runtime_acceptance.check_modules(operator)
    assert result["ok"] is True
    assert all(result["evidence"]["services"].values())
    serialized = json.dumps(result)
    for sensitive in ("secret-bot-token", "secret-ai-key", "Own seller", "private-session"):
        assert sensitive not in serialized


def test_unconfigured_disabled_services_do_not_block_aggregate_success(acceptance):
    operator, company, _, _ = acceptance
    company["subscription"] = {
        "policy": "plans_v1",
        "plan_id": "full",
        "status": "active",
        "start_date": "2020-01-01",
        "overrides": {
            feature: {"mode": "deny"}
            for feature in FEATURES
            if feature.startswith("commercial.")
            or feature
            in {"assistant.chat", "notifications.telegram", "documents.dispatch", "operations.sync"}
        },
    }
    assert runtime_acceptance.check_modules(operator)["ok"] is True


def test_bad_json_probe_is_recorded_without_discarding_other_modules(acceptance):
    operator, _, visited, failures = acceptance
    failures["/api/overview"] = "invalid-json"
    with pytest.raises(PendingCheck) as caught:
        runtime_acceptance.check_modules(operator)
    details = caught.value.details
    assert "/api/overview" in details["missing"]
    assert "/api/payment-settings" in visited
    assert "secret broken response" not in json.dumps(details)


def test_optional_database_failure_is_safe_negative_evidence(monkeypatch):
    monkeypatch.setattr(
        runtime_acceptance.psycopg,
        "connect",
        lambda *_: (_ for _ in ()).throw(psycopg.OperationalError("private database credentials")),
    )
    assert (
        runtime_acceptance.heartbeat(SimpleNamespace(config={"runtime_dsn": "secret"}), "sql")
        is False
    )


@pytest.mark.parametrize("history_from", ["2026-08-10", "2026-10-08"])
def test_dated_acceptance_probes_sample_last_imported_day_without_changing_history(
    acceptance, history_from
):
    from datetime import date

    from app.saas_admin.initial_sync_plan import initial_sync_plan

    operator, _, _, _ = acceptance
    operator.config.update(history_from=history_from, history_to="2026-10-08")
    full_history = initial_sync_plan(date.fromisoformat(history_from), date(2026, 10, 8))
    with pytest.raises(PendingCheck) as caught:
        runtime_acceptance.check_modules(operator)
    dated = {
        request.url.path: dict(request.url.params)
        for request in operator.acceptance_requests
        if "start" in request.url.params
    }
    assert set(dated) == {
        "/api/overview",
        "/api/sales/daily",
        "/api/purchase-prices",
        "/api/resources/invoices",
        "/api/resources/cash-shifts",
        "/api/resources/events",
    }
    assert all(params == {"start": "2026-10-08", "end": "2026-10-08"} for params in dated.values())
    assert caught.value.details["probe_period"] == {"start": "2026-10-08", "end": "2026-10-08"}
    assert operator.config["history_from"] == full_history["history_from"] == history_from
    assert operator.config["history_to"] == full_history["history_to"] == "2026-10-08"
    assert initial_sync_plan(date.fromisoformat(history_from), date(2026, 10, 8)) == full_history


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {"document_settings": {"commercial_enabled": "true"}},
        {"document_settings": {"commercial_seller_json": {"name": "Incomplete seller"}}},
    ],
)
def test_configuration_requires_explicit_controls_and_complete_seller(configuration):
    evidence = runtime_acceptance.configuration_evidence(configuration)
    assert evidence["commercial_enabled"] is False
    assert evidence["commercial_submit_enabled"] is False
    assert evidence["commercial_counterparty_create_enabled"] is False
    assert evidence["seller_configured"] is False
    assert evidence["assistant_configured"] is False
