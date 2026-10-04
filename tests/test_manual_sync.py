"""Manual scheduler requests use a disposable DB and never run real loaders."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_reference_sync import db as db

from app import manual_sync, scheduler
from app.core.config import Settings
from app.portal import create_portal
from app.web.settings import WebSettings
from tests.test_portal import FakeRepository, login, provider


@pytest.fixture
def ready(db):
    db.execute(
        "INSERT INTO chaika.scheduler_runtime(singleton,instance_id) VALUES(true,%s) "
        "ON CONFLICT(singleton) DO UPDATE SET available=true,heartbeat_at=clock_timestamp()",
        (uuid4(),),
    )
    return db


def request(db, job="balances", key=None, actor=None):
    return manual_sync.request_run(db, job, key or uuid4(), actor or UUID(int=1), enabled=True)


def test_durable_cooldown_and_idempotency(ready):
    key = uuid4()
    first = request(ready, key=key)
    assert first["state"] == "pending"
    assert request(ready, key=key) == first
    with pytest.raises(HTTPException) as error:
        request(ready)
    assert error.value.status_code == 409
    ready.execute("UPDATE chaika.manual_sync_requests SET state='succeeded'")
    with pytest.raises(HTTPException) as error:
        request(ready)
    assert error.value.status_code == 429
    assert 1 <= int(error.value.headers["Retry-After"]) <= 600
    assert request(ready, "events")["state"] == "pending"
    ready.execute("UPDATE chaika.manual_sync_requests SET requested_at=now()-interval '10 minutes'")
    assert request(ready)["state"] == "pending"
    for job, actor in (("events", UUID(int=1)), ("balances", UUID(int=2))):
        with pytest.raises(HTTPException) as error:
            request(ready, job, key, actor)
        assert error.value.status_code == 409


def test_unavailable_unknown_and_automatic_active(ready):
    with pytest.raises(HTTPException) as error:
        request(ready, "not-a-job")
    assert error.value.status_code == 404
    ready.execute("UPDATE chaika.scheduler_runtime SET heartbeat_at=now()-interval '1 minute'")
    with pytest.raises(HTTPException) as error:
        request(ready)
    assert error.value.status_code == 503
    ready.execute("UPDATE chaika.scheduler_runtime SET heartbeat_at=clock_timestamp()")
    ready.execute(
        "INSERT INTO chaika.scheduled_sync_runs(job,slot,status) VALUES('balances',now(),'running')"
    )
    with pytest.raises(HTTPException) as error:
        request(ready)
    assert error.value.status_code == 409
    assert not ready.execute("SELECT 1 FROM chaika.manual_sync_requests").fetchone()


def test_worker_sequential_completion_failure_and_stop(ready):
    calls = []
    first = request(ready)
    request(ready, "events")
    assert manual_sync.run_one(ready, None, Event(), execute=lambda job, *_: calls.append(job.key))
    assert calls == ["balances"]

    def failure(*_):
        raise ValueError("SECRET: must not reach UI")

    assert manual_sync.run_one(ready, None, Event(), execute=failure)
    assert not manual_sync.run_one(ready, None, Event(), execute=failure)
    rows = ready.execute(
        "SELECT state,error_code FROM chaika.manual_sync_requests ORDER BY requested_at"
    ).fetchall()
    assert rows == [("succeeded", None), ("failed", "ValueError")]
    assert request(ready, key=first["request_id"])["state"] == "succeeded"
    request(ready, "references")
    stop = Event()
    stop.set()
    assert not manual_sync.run_one(ready, None, stop, execute=failure)


def test_pending_manual_and_running_cron_cannot_overlap(ready, monkeypatch):
    job = scheduler.Job("balances", "Balances", minute=30)
    monkeypatch.setattr(scheduler, "JOBS", (job,))
    request(ready)
    calls = []
    scheduler.run_due(ready, None, Event(), execute=lambda *_: calls.append("auto"))
    assert not calls
    assert not ready.execute("SELECT 1 FROM chaika.scheduled_sync_runs").fetchone()
    manual_sync.run_one(ready, None, Event(), execute=lambda *_: calls.append("manual"))
    scheduler.run_due(ready, None, Event(), execute=lambda *_: calls.append("auto"))
    assert calls == ["manual", "auto"]


def test_state_uses_server_time_and_manual_outcome():
    now = datetime.now(UTC)
    row = dict(
        job="balances",
        state="failed",
        request_id=uuid4(),
        requested_at=now,
        started_at=now,
        finished_at=now,
        error_code="interrupted",
    )
    state = manual_sync.manual_state(row, now + timedelta(seconds=31), ready=True, authorized=True)
    assert state["remaining_seconds"] == 569
    assert state["blocked_reason"] == "cooldown" and not state["can_run"]
    assert state["error_code"] == "interrupted"
    assert manual_sync.manual_state(None, now, ready=True, authorized=True)["can_run"]
    assert (
        manual_sync.manual_state(None, now, ready=True, authorized=False)["blocked_reason"]
        == "permission"
    )
    assert (
        manual_sync.manual_state(None, now, ready=False, authorized=True)["blocked_reason"]
        == "scheduler_unavailable"
    )


def test_restart_marks_interrupted_without_replay_and_heartbeat_closes(ready):
    request(ready)
    ready.execute("UPDATE chaika.manual_sync_requests SET state='running'")

    class Collector:
        def poll(self):
            return None

    with manual_sync.heartbeat(None, ready, Collector()):
        assert manual_sync.available(ready)
        assert ready.execute(
            "SELECT state,error_code FROM chaika.manual_sync_requests"
        ).fetchone() == ("failed", "interrupted")
    assert not manual_sync.available(ready)


def test_concurrent_requests_are_global_per_job(monkeypatch):
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated DB required")
    assert "127.0.0.1:15438/" in url
    job = "test_" + uuid4().hex
    monkeypatch.setattr(scheduler, "JOBS", (scheduler.Job(job, "Test"),))
    with psycopg.connect(url) as connection:
        connection.execute(
            "INSERT INTO chaika.scheduler_runtime(singleton,instance_id) VALUES(true,%s) "
            "ON CONFLICT(singleton) DO UPDATE SET available=true,heartbeat_at=clock_timestamp()",
            (uuid4(),),
        )
    barrier = Barrier(2)

    def attempt(index):
        with psycopg.connect(url) as connection:
            connection.execute("SET LOCAL ROLE chaika_backend")
            barrier.wait()
            try:
                return request(connection, job, actor=UUID(int=index + 1))["state"]
            except HTTPException as error:
                return error.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert set(executor.map(attempt, range(2))) == {"pending", 409}
        with psycopg.connect(url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM chaika.manual_sync_requests WHERE job=%s", (job,)
                ).fetchone()[0]
                == 1
            )
    finally:
        with psycopg.connect(url) as connection:
            connection.execute("DELETE FROM chaika.manual_sync_requests WHERE job=%s", (job,))
            connection.execute("DELETE FROM chaika.scheduler_runtime")


def test_public_endpoint_requires_admin_section_and_csrf():
    class Repo(FakeRepository):
        admin = True
        sections = ["status"]
        calls = []

        def scope(self, user_id, selected=None):
            scope = super().scope(user_id, selected)
            return replace(
                scope, user={**scope.user, "is_portal_admin": self.admin, "sections": self.sections}
            )

        def manual_sync(self, scope, job, request_id):
            self.calls.append(job)
            return {"state": "pending", "job": job, "request_id": str(request_id)}

    repo = Repo()
    app = create_portal(
        Settings(_env_file=None),
        WebSettings(_env_file=None, anon_key="test"),
        repository=repo,
        auth_transport=httpx.MockTransport(provider),
    )
    with TestClient(app, base_url="http://127.0.0.1:8013") as client:
        path = "/api/status/sync/balances/run"
        payload = {"request_id": str(uuid4())}
        assert client.post(path, json=payload).status_code == 401
        login(client)
        assert client.post(path, json=payload).status_code == 403
        headers = {"Origin": "http://127.0.0.1:8013"}
        repo.admin = False
        assert client.post(path, json=payload, headers=headers).status_code == 403
        repo.admin = True
        repo.sections = []
        assert client.post(path, json=payload, headers=headers).status_code == 403
        repo.sections = ["status"]
        assert client.post(path, json={"request_id": "bad"}, headers=headers).status_code == 422
        assert client.post(path, json=payload, headers=headers).status_code == 202
        assert repo.calls == ["balances"]


def test_status_exposes_every_job_with_manual_result(ready):
    from contextlib import contextmanager

    from psycopg.rows import dict_row

    from app.web.repository import Repository, Scope

    doc = request(ready)
    ready.execute("UPDATE chaika.manual_sync_requests SET state='failed',error_code='interrupted'")

    class Repo:
        settings = Settings(_env_file=None, sync_enabled=True)

        @contextmanager
        def connection(self):
            previous = ready.row_factory
            ready.row_factory = dict_row
            try:
                yield ready
            finally:
                ready.row_factory = previous

    scope = Scope({"id": UUID(int=1), "role": "owner", "is_portal_admin": True}, (), None, (), ())
    status = Repository.status(Repo(), scope)
    assert len(status["scheduled"]) == len(scheduler.JOBS)
    balances = next(row for row in status["scheduled"] if row["job"] == "balances")
    assert balances["status"] == "waiting"
    assert balances["scheduler_available"]
    assert balances["manual"]["request_id"] == str(doc["request_id"])
    assert balances["manual"]["state"] == "failed"
    assert balances["manual"]["error_code"] == "interrupted"
    assert balances["manual"]["blocked_reason"] == "cooldown"


def test_execution_uses_start_day_after_waiting_across_midnight(ready):
    request(ready)
    ready.execute("UPDATE chaika.manual_sync_requests SET requested_at=now()-interval '2 days'")
    times = []
    manual_sync.run_one(ready, None, Event(), execute=lambda job, slot, *_: times.append(slot))
    started = ready.execute("SELECT started_at FROM chaika.manual_sync_requests").fetchone()[0]
    assert times == [started]
    assert datetime.now(UTC) - started < timedelta(seconds=10)
