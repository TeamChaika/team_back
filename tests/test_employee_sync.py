import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_iiko_employees import config, employee, wrap
from test_reference_sync import db as db

from app import sync_employees
from app.api.routes import sync_jobs as routes
from app.main import create_app
from app.schemas.iiko_employees import IikoEmployeesSnapshot
from app.services.iiko_employees import read_employees, summarize_employees
from app.sync_references import Source, SyncError, append_snapshot, register_sources


@pytest.fixture
def sample(tmp_path):
    settings = config()
    source = Source("primary", "Chain", str(settings.iiko_base_url).rstrip("/"), "a" * 64)
    folder = tmp_path / ".local/employees"
    folder.mkdir(parents=True)
    raw = wrap(employee() + employee(2, ""))
    key = uuid4()
    path = folder / f"{key}.xml"
    path.write_bytes(raw)
    rows = read_employees(path, len(raw))
    response = IikoEmployeesSnapshot(
        snapshot_id=key,
        received_at=datetime.now(UTC),
        total=len(rows),
        source_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        **summarize_employees(rows),
    ).model_dump(mode="json")
    metadata = {"source_fingerprint": source.fingerprint, "snapshot": response}
    (folder / "current.json").write_text(json.dumps(metadata))
    return settings, source, folder, response


def capture(sample):
    settings, source, folder, response = sample
    return sync_employees.capture_employees(settings, source, response, folder)


def test_capture_preserves_verified_raw_but_normalized_fields_exclude_credentials(sample):
    snapshot = capture(sample)
    assert snapshot["resource"] == "employees"
    assert b"private-password" in snapshot["raw"]
    assert "private-password" not in json.dumps(snapshot["payload"])
    assert snapshot["payload"]["items"][1]["code"] == ""


@pytest.mark.parametrize("changed", ["source", "metadata", "raw", "summary"])
def test_capture_rejects_mismatched_source_snapshot_hash_and_counts(sample, changed):
    _, _, folder, response = sample
    path = folder / "current.json"
    metadata = json.loads(path.read_text())
    if changed == "source":
        metadata["source_fingerprint"] = "b" * 64
    elif changed == "metadata":
        metadata["snapshot"]["total"] = 99
    elif changed == "raw":
        (folder / f"{response['snapshot_id']}.xml").write_bytes(b"wrong")
    else:
        response["without_code"] = 99
        metadata["snapshot"] = response
    path.write_text(json.dumps(metadata))
    with pytest.raises(SyncError):
        capture(sample)


def test_department_states_and_null_members_are_preserved(tmp_path):
    raw = wrap(
        employee(
            extra="<responsibilityDepartmentCodesState>NULL</responsibilityDepartmentCodesState>"
        )
    )
    raw = raw.replace(
        b"<departmentCodes>0001</departmentCodes><departmentCodes>0002</departmentCodes>",
        b"<departmentCodesState>EMPTY</departmentCodesState>",
    )
    path = tmp_path / "states.xml"
    path.write_bytes(raw)
    row = read_employees(path, len(raw))[0]
    assert row.department_codes is None and row.department_codes_state == "EMPTY"
    assert row.responsibility_department_codes_state == "NULL"


def stage(db, sample):
    _, source, _, _ = sample
    register_sources(db, [source])
    snapshot = capture(sample)
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'employees','running')", (run,)
    )
    append_snapshot(db, run, snapshot)
    return run, snapshot


def test_upsert_retains_arrays_nulls_and_duplicate_codes(db, sample):
    _, snapshot = stage(db, sample)
    first, second = snapshot["payload"]["items"]
    second["code"] = first["code"]
    first["department_codes"] = ["0001", None, "0001", ""]
    first["role_ids"] = [str(UUID(int=99)), None]
    second["role_ids"] = []
    second["department_codes"] = None
    second["department_codes_state"] = "NULL"
    count = sync_employees.publish_employees(db, snapshot)
    assert count["employees"] == 2
    sync_employees.publish_employees(db, snapshot)
    assert db.execute("SELECT count(*) FROM chaika.employees").fetchone()[0] == 2
    rows = db.execute(
        "SELECT role_ids,department_codes,department_codes_state FROM chaika.employees ORDER BY id"
    ).fetchall()
    assert rows == [([UUID(int=99), None], ["0001", None, "0001", ""], None), ([], None, "NULL")]


def test_absence_keeps_flags_and_return_restores_presence_with_raw_history(db, sample):
    run, first = stage(db, sample)
    sync_employees.publish_employees(db, first)
    for index, item_count in [(1, 1), (2, 2)]:
        snapshot = deepcopy(first)
        snapshot["id"] = str(uuid4())
        snapshot["observed_at"] += timedelta(seconds=index)
        snapshot["payload"]["items"] = snapshot["payload"]["items"][:item_count]
        snapshot["payload"]["total"] = item_count
        append_snapshot(db, run, snapshot)
        result = sync_employees.publish_employees(db, snapshot)
        assert result["absent_from_latest"] == 2 - item_count
        row = db.execute(
            "SELECT present_in_latest,deleted,first_seen_at FROM chaika.employees WHERE id=%s",
            (UUID(int=2),),
        ).fetchone()
        assert row == (item_count == 2, False, first["observed_at"])
    assert (
        db.execute(
            "SELECT count(*) FROM chaika.raw_snapshots WHERE resource='employees'"
        ).fetchone()[0]
        == 3
    )


def test_empty_or_duplicate_export_does_not_clear_current_dictionary(db, sample):
    _, snapshot = stage(db, sample)
    sync_employees.publish_employees(db, snapshot)
    for items in ([], [snapshot["payload"]["items"][0]] * 2):
        broken = deepcopy(snapshot)
        broken["payload"]["items"] = items
        broken["payload"]["total"] = len(items)
        with pytest.raises(SyncError):
            sync_employees.publish_employees(db, broken)
    assert (
        db.execute("SELECT count(*) FROM chaika.employees WHERE present_in_latest").fetchone()[0]
        == 2
    )


def test_failed_publication_restores_presence(db, sample):
    _, snapshot = stage(db, sample)
    sync_employees.publish_employees(db, snapshot)
    broken = deepcopy(snapshot)
    broken["id"] = str(uuid4())  # Invalid snapshot FK forces a publication rollback.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sync_employees.publish_employees(db, broken)
    assert (
        db.execute("SELECT count(*) FROM chaika.employees WHERE present_in_latest").fetchone()[0]
        == 2
    )


def test_private_table_has_no_public_read_or_backend_delete(db, sample):
    _, snapshot = stage(db, sample)
    sync_employees.publish_employees(db, snapshot)
    assert db.execute(
        "SELECT relrowsecurity FROM pg_class WHERE oid='chaika.employees'::regclass"
    ).fetchone()[0]
    for role in ("anon", "authenticated"):
        assert not db.execute(
            "SELECT has_table_privilege(%s,'chaika.employees','SELECT')", (role,)
        ).fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("DELETE FROM chaika.employees")


@pytest.fixture
def runtime(db, sample, monkeypatch):
    settings, source, folder, response = sample
    settings = config(database_url="test-only")
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    monkeypatch.setattr(sync_employees, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync_employees, "BACKEND_DIR", folder.parent.parent)
    monkeypatch.setattr(sync_employees.psycopg, "connect", lambda *a, **k: nullcontext(db))
    calls = []
    responses = {"load": 200, "logout": 200}

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/load"):
            return httpx.Response(responses["load"], json=response)
        assert request.url.path.endswith("/logout")
        return httpx.Response(responses["logout"], json={"state": "logged_out"})

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://127.0.0.1:8010")
    monkeypatch.setattr(sync_employees.httpx, "Client", lambda **kwargs: client)
    return settings, db, calls, responses


def test_sync_commits_raw_rows_and_success_together_then_logs_out(runtime):
    settings, db, calls, _ = runtime
    result = sync_employees.synchronize_employees(settings)
    assert result["status"] == "succeeded" and result["counts"]["logout_ok"] is True
    assert result["counts"]["employees"] == 2 and calls[-1].endswith("/logout")
    assert (
        db.execute(
            "SELECT status FROM chaika.sync_runs WHERE id=%s", (result["run_id"],)
        ).fetchone()[0]
        == "succeeded"
    )
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["load", "logout", "publish"])
def test_sync_failure_does_not_publish_and_logout_is_attempted(runtime, monkeypatch, failure):
    settings, db, calls, responses = runtime
    if failure == "publish":

        def fail(*args):
            raise ValueError("private-password")

        monkeypatch.setattr(sync_employees, "publish_employees", fail)
    else:
        responses[failure] = 502
    with pytest.raises(SyncError) as error:
        sync_employees.synchronize_employees(settings)
    assert "private-password" not in str(error.value)
    assert calls[-1].endswith("/logout")
    assert db.execute("SELECT count(*) FROM chaika.employees").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "failed"


def test_api_requires_key_and_hides_failure_details(monkeypatch):
    settings = config(sync_api_key="test-sync-key")
    calls = []

    def fail(_):
        calls.append(1)
        raise SyncError("private-source-path")

    monkeypatch.setattr(routes, "synchronize_employees", fail)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/sync/employees").status_code == 401
        assert not calls
        response = client.post("/api/v1/sync/employees", headers={"X-Sync-Key": "test-sync-key"})
        assert response.status_code == 503 and "private-source-path" not in response.text
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/api/v1/sync/employees"]["post"]["security"] == [{"SyncKey": []}]


def test_api_returns_conflict_for_shared_sync_lock(monkeypatch):
    def busy(_):
        raise SyncError("sync_already_running")

    monkeypatch.setattr(routes, "synchronize_employees", busy)
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as client:
        response = client.post("/api/v1/sync/employees", headers={"X-Sync-Key": "test-sync-key"})
        assert response.status_code == 409
