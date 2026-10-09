import pytest

from app.saas_admin.entitlements import FEATURES
from app.saas_admin.feature_readiness import (
    DEPENDENCIES,
    feature_readiness,
    require_feature_ready,
)


@pytest.fixture
def accepted():
    company = {
        "id": "f7251991-293e-43c1-a9f5-8505a4cc11bc",
        "version": 7,
        "status": "active",
        "modules": {
            "analytics": True,
            "documents": True,
            "commercial_invoices": True,
            "deposits": True,
        },
    }
    paths = {"/api/me"} | {path for deps in DEPENDENCIES.values() for path in deps.paths}
    services = {
        service: True
        for deps in DEPENDENCIES.values()
        for service in (*deps.read_services, *(deps.write_services or ()))
    }
    checks = {
        "modules": {
            "ok": True,
            "evidence": {
                "company_id": company["id"],
                "configuration_version": 7,
                "probes": [{"path": path, "ok": True, "status": 200} for path in paths],
                "services": services,
            },
        },
        "connections": {"ok": True, "evidence": {"checked_connections": 1}},
        "payments": {
            "ok": True,
            "evidence": {
                "terminals": [
                    {
                        "terminal_id": "terminal",
                        "terminal_version_id": "terminal-version",
                        "check_id": "check",
                        "settled_attempt_id": "paid-attempt",
                        "configuration_version": 7,
                        "mode": "sandbox",
                    }
                ]
            },
        },
    }
    return company, checks


def test_every_catalog_feature_has_explicit_policy_and_unknown_denied(accepted):
    company, checks = accepted
    readiness = feature_readiness(company, checks, working=True)
    assert set(DEPENDENCIES) == set(FEATURES) == set(readiness)
    assert all(item["state"] == "ready" and item["read"] for item in readiness.values())
    assert readiness["payments.create"]["write"] is True
    assert readiness["analytics.overview"]["write"] is False
    assert require_feature_ready(readiness, "profile.account", write=True)
    assert not require_feature_ready(readiness, "finance.history")
    assert not require_feature_ready({"finance.history": {"read": True}}, "finance.history")


@pytest.mark.parametrize("working,version", [(False, 7), (True, 6)])
def test_unavailable_or_stale_runtime_cannot_authorize_any_feature(accepted, working, version):
    company, checks = accepted
    checks["modules"]["evidence"]["configuration_version"] = version
    readiness = feature_readiness(company, checks, working=working)
    assert all(not item["read"] and not item["write"] for item in readiness.values())


def test_foreign_company_and_absent_evidence_fail_closed(accepted):
    company, checks = accepted
    checks["modules"]["evidence"]["company_id"] = "another-company"
    assert not feature_readiness(company, checks, working=True)["profile.account"]["read"]
    assert all(
        item["state"] == "not_checked"
        for item in feature_readiness(company, {}, working=True).values()
    )


def test_optional_modules_do_not_block_document_drafts_approval_or_analytics(accepted):
    company, checks = accepted
    checks["modules"]["ok"] = False
    services = checks["modules"]["evidence"]["services"]
    for name in ("telegram_identity", "assistant_configured", "seller_configured"):
        services[name] = False
    checks["payments"] = {"ok": False}
    readiness = feature_readiness(company, checks, working=True)
    for feature in (
        "documents.waybills",
        "documents.writeoffs",
        "documents.approval",
        "documents.dispatch",
        "deposits.bookings",
        "management.settings",
    ):
        assert readiness[feature]["write"] is True, (feature, readiness[feature])
    assert readiness["analytics.overview"]["read"] is True
    assert readiness["payments.status"]["read"] is True
    for feature in ("notifications.telegram", "assistant.chat", "commercial.incoming"):
        assert readiness[feature]["read"] is False
    assert readiness["payments.create"]["write"] is False


def test_get_probes_do_not_authorize_external_commands_without_service_evidence(accepted):
    company, checks = accepted
    checks["modules"]["evidence"]["services"] = {}
    checks.pop("connections")
    checks.pop("payments")
    readiness = feature_readiness(company, checks, working=True)
    for feature in (
        "documents.dispatch",
        "commercial.incoming",
        "commercial.outgoing",
        "commercial.counterparties",
        "assistant.chat",
        "notifications.telegram",
        "employees.management",
        "payments.create",
        "operations.sync",
    ):
        assert readiness[feature]["write"] is False, feature
    assert readiness["documents.dispatch"]["read"] is True
    assert readiness["documents.waybills"]["write"] is True


@pytest.mark.parametrize(
    "flag,feature",
    [
        ("commercial_submit_enabled", "commercial.incoming"),
        ("commercial_submit_enabled", "commercial.outgoing"),
        ("commercial_counterparty_create_enabled", "commercial.counterparties"),
        ("documents_worker", "documents.dispatch"),
        ("scheduler", "operations.sync"),
    ],
)
def test_disabled_operator_control_blocks_writes_preserves_reads(accepted, flag, feature):
    company, checks = accepted
    checks["modules"]["evidence"]["services"][flag] = False
    item = feature_readiness(company, checks, working=True)[feature]
    assert item["read"] is True
    assert item["write"] is False
    assert item["state"] == "blocked"


@pytest.mark.parametrize(
    "proof",
    [
        {"ok": True, "evidence": {"enabled": False}},
        {"ok": True, "evidence": {"terminals": []}},
        {"ok": False, "evidence": {"terminals": []}},
    ],
)
def test_disabled_or_incomplete_payment_acceptance_cannot_create(accepted, proof):
    company, checks = accepted
    checks["payments"] = proof
    readiness = feature_readiness(company, checks, working=True)
    assert readiness["payments.create"]["write"] is False
    assert readiness["payments.status"]["read"] is True
    assert readiness["deposits.bookings"]["write"] is True


@pytest.mark.parametrize(
    "key,value",
    [
        ("configuration_version", 6),
        ("mode", "unknown"),
        ("terminal_version_id", ""),
        ("settled_attempt_id", None),
        ("check_id", ""),
    ],
)
def test_payment_terminal_proof_must_be_complete_and_current(accepted, key, value):
    company, checks = accepted
    checks["payments"]["evidence"]["terminals"][0][key] = value
    assert not feature_readiness(company, checks, working=True)["payments.create"]["write"]


def test_expired_subscription_keeps_history_but_denies_writes(accepted):
    company, checks = accepted
    company["subscription"] = {
        "policy": "plans_v1",
        "plan_id": "full",
        "status": "active",
        "start_date": "2020-01-01",
        "end_date": "2020-01-02",
    }
    readiness = feature_readiness(company, checks, working=True)
    assert readiness["documents.waybills"]["read"] is True
    assert readiness["documents.waybills"]["write"] is False


def test_failed_probe_is_scoped_and_all_duplicates_must_pass(accepted):
    company, checks = accepted
    checks["modules"]["evidence"]["probes"].append(
        {"path": "/api/purchase-prices", "ok": False, "status": 503}
    )
    readiness = feature_readiness(company, checks, working=True)
    assert not readiness["purchases.prices"]["read"]
    assert not readiness["purchases.impact"]["read"]
    assert readiness["analytics.overview"]["read"]
