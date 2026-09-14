import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import sync_refresh
from app.core.config import Settings
from app.main import create_app
from app.services import sync_jobs
from app.sync_references import SyncError

KEY = "test-only-refresh-key"
HEADERS = {"X-Sync-Key": KEY}


@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_url="private", sync_api_key=KEY)
    app = create_app(settings, sync_directory=tmp_path)
    service = app.state.sync_jobs
    calls = []
    monkeypatch.setattr(service, "preflight", lambda: None)
    monkeypatch.setattr(service, "spawn", lambda job_id: calls.append(job_id) or 321)
    monkeypatch.setattr(sync_jobs, "job_alive", lambda pid, job_id: pid == 321)
    with TestClient(app) as client:
        yield client, service, calls


def start(client, job_id):
    return client.post("/api/v1/sync/refresh", json={"request_id": str(job_id)}, headers=HEADERS)


def test_authorization_precedes_launch_and_rejects_unconfigured_key(api):
    client, service, calls = api
    for headers in ({}, {"X-Sync-Key": "wrong"}):
        response = client.post("/api/v1/sync/refresh", json={}, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "sync_unauthorized"
    assert calls == []
    with TestClient(create_app(Settings(_env_file=None))) as unconfigured:
        assert unconfigured.post("/api/v1/sync/refresh", json={}).status_code == 503
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/v1/sync/refresh"]["post"]["security"] == [{"SyncKey": []}]


@pytest.mark.parametrize(
    "timestamp,end",
    [
        (datetime(2026, 9, 10, 20, tzinfo=UTC), date(2026, 9, 10)),
        (datetime(2026, 9, 10, 21, 5, tzinfo=UTC), date(2026, 9, 11)),
        (datetime(2024, 3, 1, 10, tzinfo=UTC), date(2024, 3, 1)),
    ],
)
def test_period_has_60_inclusive_local_days(api, monkeypatch, timestamp, end):
    client, service, calls = api
    monkeypatch.setattr(sync_jobs, "now", lambda: timestamp)
    result = start(client, uuid4())
    assert result.status_code == 202
    data = result.json()
    assert data["date_to"] == end.isoformat()
    assert (date.fromisoformat(data["date_to"]) - date.fromisoformat(data["date_from"])).days == 59
    assert data["days"] == 60 and len(calls) == 1
    assert all(r["total_days"] == 60 for r in data["resources"])


def test_request_id_survives_service_restart_and_new_id_is_blocked_while_running(api):
    client, service, calls = api
    job_id = uuid4()
    first = start(client, job_id)
    assert first.status_code == 202
    assert first.headers["location"] == f"/api/v1/sync/jobs/{job_id}"
    assert start(client, job_id).status_code == 200
    restarted = sync_jobs.SyncJobsService(service.settings, service.directory)
    result, created = restarted.start(job_id)
    assert not created and result.job_id == job_id and len(calls) == 1
    assert start(client, uuid4()).status_code == 409
    status = client.get(first.headers["location"], headers=HEADERS)
    assert status.status_code == 200
    assert all(private not in status.text for private in (KEY, "private", "pid", "worker.log"))
    assert client.get(first.headers["location"]).status_code == 401


def test_launch_lock_prevents_concurrent_processes(api, monkeypatch):
    _, service, calls = api
    entered, release = Event(), Event()

    def spawn(job_id):
        calls.append(job_id)
        entered.set()
        assert release.wait(5)
        return 321

    monkeypatch.setattr(service, "spawn", spawn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.start, uuid4())
        assert entered.wait(5)
        try:
            with pytest.raises(sync_jobs.SyncJobError, match="sync_start_busy"):
                service.start(uuid4())
        finally:
            release.set()
        assert future.result()[1]
    assert len(calls) == 1


def test_dead_worker_is_interrupted_and_new_job_can_start(api, monkeypatch):
    client, service, _ = api
    job_id = uuid4()
    start(client, job_id)
    monkeypatch.setattr(sync_jobs, "job_alive", lambda *args: False)
    later = sync_jobs.now() + timedelta(seconds=31)
    monkeypatch.setattr(sync_jobs, "now", lambda: later)
    response = client.get(f"/api/v1/sync/jobs/{job_id}", headers=HEADERS)
    assert response.json()["status"] == "interrupted"
    assert response.json()["error_code"] == "sync_worker_interrupted"
    assert start(client, uuid4()).status_code == 202


def test_worker_launch_failure_is_persisted_and_retry_does_not_spawn_again(api, monkeypatch):
    client, service, _ = api
    job_id = uuid4()

    def broken(_):
        raise OSError("private-path-and-secret")

    monkeypatch.setattr(service, "spawn", broken)
    response = start(client, job_id)
    assert response.status_code == 503 and "private-path" not in response.text
    repeat = start(client, job_id)
    assert repeat.status_code == 200 and repeat.json()["status"] == "failed"


def test_validation_missing_job_and_corrupt_state_fail_closed(api):
    client, service, _ = api
    assert (
        client.post(
            "/api/v1/sync/refresh", json={"date_from": "2023-01-01"}, headers=HEADERS
        ).status_code
        == 422
    )
    assert client.get(f"/api/v1/sync/jobs/{uuid4()}", headers=HEADERS).status_code == 404
    job_id = uuid4()
    start(client, job_id)
    (service.job_directory(job_id) / "job.json").write_text("private broken json")
    response = client.get(f"/api/v1/sync/jobs/{job_id}", headers=HEADERS)
    assert response.status_code == 503 and "private" not in response.text


def test_pid_identity_requires_exact_module_and_job_id(monkeypatch):
    job_id = uuid4()
    monkeypatch.setattr(
        sync_jobs.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout=f"S /venv/python -u -m app.sync_refresh --job-id {job_id}"
        ),
    )
    assert sync_jobs.job_alive(321, job_id)
    assert not sync_jobs.job_alive(321, uuid4())
    assert not sync_jobs.job_alive(None, job_id)


def test_worker_failure_has_safe_persistent_status(api, monkeypatch):
    client, service, _ = api
    job_id = uuid4()
    start(client, job_id)
    requests = []

    def fail(settings, url, start, end, jobs, stop, *, on_ready):
        requests.extend(jobs)
        assert (end - start).days == 59
        raise SyncError("upstream_unavailable")

    monkeypatch.setattr(sync_refresh, "synchronize_histories", fail)
    with pytest.raises(SyncError, match="upstream_unavailable"):
        sync_refresh.run_job(service, job_id, Event())
    data = json.loads((service.job_directory(job_id) / "job.json").read_text())
    assert data["status"] == "failed" and data["finished_at"]
    assert [job.resource for job in requests] == ["writeoffs", "incoming_invoices"]
    assert all(job.mirror.parent == service.directory for job in requests)
    assert not (service.directory / "invoices-process.json").exists()
