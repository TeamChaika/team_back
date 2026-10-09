from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.saas_admin.provisioning import STEPS
from app.saas_admin.provisioning_routes import ProvisioningRequests, public_targets
from app.saas_admin.repository import Repository
from app.saas_admin.server import BASE, create_app


def test_readiness_requires_current_full_evidence_and_hides_private_data():
    company = {"id": str(uuid4()), "version": 3, "domain": "tenant.example.org"}
    service = ProvisioningRequests(
        object(),
        "/private/run",
        {
            "frontend": {"type": "CNAME", "value": "static.example.org"},
            "api": {"type": "A", "value": "8.8.8.8"},
        },
    )
    row = {
        "configuration_version": 3,
        "state": "ready",
        "step": None,
        "updated_at": datetime.now(UTC),
        "socket_path": "/private/socket",
        "checks": {step: {"ok": True, "evidence": "sensitive operator evidence"} for step in STEPS},
    }
    result = service.present(company, row)
    assert result["ready"] is True
    assert result["dns_records"][1]["name"] == "api.tenant.example.org"
    assert "sensitive" not in str(result) and "/private" not in str(result)
    row["checks"]["payments"] = {"ok": True}
    assert service.present(company, row)["ready"] is False
    row["configuration_version"] = 2
    assert service.present(company, row)["state"] == "not_started"


@pytest.mark.parametrize(
    "targets",
    [
        {"api": {"type": "A", "value": "127.0.0.1"}},
        {"api": {"type": "A", "value": "10.0.0.1"}},
        {"frontend": {"type": "CNAME", "value": "https://static.example.org"}},
    ],
)
def test_public_dns_settings_fail_closed(targets):
    with pytest.raises(ValueError):
        public_targets(targets)


def test_owner_csrf_and_explicit_operator_unconfigured(tmp_path):
    origin = "http://127.0.0.1:8210"
    repo = Repository(tmp_path / "data")
    repo.bootstrap("owner", "long-owner-password", "Owner")
    client = TestClient(
        create_app(tmp_path / "data", repository=repo), base_url=origin, headers={"Origin": origin}
    )
    assert client.get(BASE + "/companies/unknown/provisioning").status_code == 401
    login = client.post(
        BASE + "/auth/login", json={"username": "owner", "password": "long-owner-password"}
    ).json()
    client.headers["X-CSRF-Token"] = login["csrf_token"]
    company = client.post(
        BASE + "/companies",
        json={
            "name": "Test",
            "slug": "test",
            "subscription": {
                "policy": "plans_v1",
                "plan_id": "full",
                "start_date": "2026-01-01",
                "end_date": "2099-12-31",
            },
        },
    ).json()
    path = BASE + "/companies/" + company["id"] + "/provisioning"
    assert client.get(path).json()["state"] == "not_started"
    assert client.get(path).json()["configured"] is False
    for action in ("start", "retry"):
        assert client.post(path + "/" + action, json={"expected_version": 1}).status_code == 503
        assert client.post(path + "/" + action, json={"expected_version": True}).status_code == 422
    del client.headers["X-CSRF-Token"]
    assert client.post(path + "/start", json={"expected_version": 1}).status_code == 403


# The imported fixture requires an explicit disposable loopback PostgreSQL DSN.
from test_provisioning_postgres import control  # noqa: E402,F401


def test_durable_requests_deduplicate_resume_and_reject_stale_version(control):  # noqa: F811
    from app.saas_admin.repository import Problem

    company = {"id": str(uuid4()), "version": 1, "domain": "tenant.example.org"}
    control.get = lambda company_id: dict(company)
    control._get = lambda db, company_id: dict(company)
    control._require_owner = lambda db, actor: None
    service = ProvisioningRequests(control, "/private/run")
    assert service.enqueue(company["id"], 1, {})["state"] == "pending"
    first_time = service.status(company["id"])["updated_at"]
    assert service.enqueue(company["id"], 1, {})["updated_at"] == first_time
    with control.connect(True) as db:
        db.execute(
            "UPDATE runtime_provisioning SET state='failed',step='connections', "
            "checks='{" + '"migrations":{"ok":true,"evidence":"test"}' + "}'::jsonb"
        )
    with pytest.raises(Problem) as failure:
        service.enqueue(company["id"], 1, {})
    assert failure.value.code == "retry_required"
    resumed = service.enqueue(company["id"], 1, {}, retry=True)
    assert resumed["state"] == "pending"
    assert next(step for step in resumed["steps"] if step["id"] == "migrations")["completed"]
    company["version"] = 2
    with pytest.raises(Problem) as stale:
        service.enqueue(company["id"], 1, {}, retry=True)
    assert stale.value.code == "version_conflict"
    new = service.enqueue(company["id"], 2, {})
    assert not any(step["completed"] for step in new["steps"])
    with control.connect() as db:
        db.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))", ("provision:" + company["id"],)
        )
        try:
            with pytest.raises(Problem) as busy:
                service.enqueue(company["id"], 2, {})
            assert busy.value.code == "provisioning_busy"
        finally:
            db.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))", ("provision:" + company["id"],)
            )
