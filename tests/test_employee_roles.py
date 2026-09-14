"""Synthetic role contracts, UUID joins, atomic publication and private access."""

import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_iiko_employees import config
from test_reference_sync import db as db

from app import sync_employee_roles as sync
from app.api.routes import iiko_employee_roles as routes
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.schemas.iiko_employee_roles import EmployeeRolesSnapshot
from app.services.iiko_employee_roles import read_employee_roles, summarize_employee_roles
from app.sync_references import Source, SyncError, append_snapshot, register_sources

HEADERS = {"X-Sync-Key": "test-sync-key"}


def role(i=1, code="", extra="", deleted="false"):
    return (
        f"<role><id>{UUID(int=i)}</id><code>{code}</code><name>Role {i}</name>"
        "<paymentPerHour>123.123456789123456789</paymentPerHour><steadySalary>0.000</steadySalary>"
        f"<scheduleType>FUTURE_TYPE</scheduleType><deleted>{deleted}</deleted>{extra}</role>"
    )


def xml(body):
    return (
        '<employeeRoles xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + body
        + "</employeeRoles>"
    ).encode()


def read(tmp_path, raw):
    path = tmp_path / "roles.xml"
    path.write_bytes(raw)
    return read_employee_roles(path, len(raw))


def test_decimal_empty_codes_deleted_and_future_fields_are_preserved(tmp_path):
    rows = read(tmp_path, xml(role(extra="<future><a>1</a></future>") + role(2, deleted="true")))
    assert rows[0].payment_per_hour == Decimal("123.123456789123456789")
    assert rows[0].model_dump(mode="json")["steady_salary"] == "0.000"
    assert rows[0].schedule_type == "FUTURE_TYPE"
    assert rows[0].code == rows[1].code == "" and rows[1].deleted is True
    assert summarize_employee_roles(rows)["without_code"] == 2


@pytest.mark.parametrize(
    "body",
    [
        role() + role(),
        role(extra="<name>Duplicate</name>"),
        role(deleted="yes"),
        role().replace("123.123456789123456789", "NaN"),
        role().replace("123.123456789123456789", "Infinity"),
        role().replace(str(UUID(int=1)), "bad-id"),
        role().replace("<name>Role 1</name>", '<name xsi:nil="true">content</name>'),
    ],
)
def test_invalid_catalogue_never_partially_publishes(tmp_path, body):
    with pytest.raises(IikoError, match="XML"):
        read(tmp_path, xml(body))


def test_nil_zero_and_negative_amounts_are_distinct_and_xxe_rejected(tmp_path):
    raw = xml(role()).replace(
        b"<steadySalary>0.000</steadySalary>", b'<steadySalary xsi:nil="true"/>'
    )
    raw = raw.replace(b"123.123456789123456789", b"-1.234")
    row = read(tmp_path, raw)[0]
    assert row.steady_salary is None and row.payment_per_hour == Decimal("-1.234")
    with pytest.raises(IikoError):
        read(tmp_path, b'<!DOCTYPE x [<!ENTITY e "x">]><employeeRoles/>')
    with pytest.raises(IikoError):
        read(tmp_path, b"<employees/>")


@pytest.fixture
def sample(tmp_path):
    settings = config(sync_api_key="test-sync-key")
    source = Source("primary", "Chain", str(settings.iiko_base_url).rstrip("/"), "a" * 64)
    folder = tmp_path / ".local/employee-roles"
    folder.mkdir(parents=True)
    raw = xml(role() + role(2))
    key = uuid4()
    path = folder / f"{key}.xml"
    path.write_bytes(raw)
    rows = read_employee_roles(path, len(raw))
    response = EmployeeRolesSnapshot(
        snapshot_id=key,
        received_at=datetime.now(UTC),
        total=len(rows),
        source_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        **summarize_employee_roles(rows),
    ).model_dump(mode="json")
    (folder / "current.json").write_text(
        json.dumps(
            {
                "source_fingerprint": source.fingerprint,
                "snapshot": response,
            }
        )
    )
    return settings, source, folder, response


@pytest.mark.parametrize("change", ["source", "raw", "revision", "count"])
def test_capture_rejects_wrong_source_raw_revision_or_count(sample, change):
    settings, source, folder, response = sample
    if change == "source":
        source = Source(source.id, source.label, source.base_url, "b" * 64)
    elif change == "raw":
        (folder / f"{response['snapshot_id']}.xml").write_bytes(b"wrong")
    else:
        response["revision_from" if change == "revision" else "total"] = 999
        (folder / "current.json").write_text(
            json.dumps(
                {
                    "source_fingerprint": source.fingerprint,
                    "snapshot": response,
                }
            )
        )
    with pytest.raises((SyncError, ValueError)):
        sync.capture_employee_roles(settings, source, response, folder)


def stage(db, sample):
    settings, source, folder, response = sample
    register_sources(db, [source])
    snapshot = sync.capture_employee_roles(settings, source, response, folder)
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'employee_roles','running')", (run,)
    )
    append_snapshot(db, run, snapshot)
    return snapshot


def test_repeat_absence_and_stale_snapshot_preserve_data(db, sample):
    snapshot = stage(db, sample)
    assert sync.publish_employee_roles(db, snapshot)["employee_roles"] == 2
    first = db.execute("SELECT first_seen_at FROM chaika.employee_roles LIMIT 1").fetchone()[0]
    newer = deepcopy(snapshot)
    newer["observed_at"] += timedelta(seconds=1)
    assert sync.publish_employee_roles(db, newer)["employee_roles"] == 2
    assert db.execute("SELECT count(*) FROM chaika.employee_roles").fetchone()[0] == 2
    assert (
        db.execute("SELECT first_seen_at FROM chaika.employee_roles LIMIT 1").fetchone()[0] == first
    )
    newer["payload"]["items"].pop()
    newer["payload"]["total"] = 1
    assert sync.publish_employee_roles(db, newer)["absent_from_latest"] == 1
    assert (
        db.execute(
            "SELECT deleted FROM chaika.employee_roles WHERE id=%s", (UUID(int=2),)
        ).fetchone()[0]
        is False
    )
    with pytest.raises(SyncError, match="stale"):
        sync.publish_employee_roles(db, snapshot)
    broken = deepcopy(newer)
    broken["id"] = str(uuid4())
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sync.publish_employee_roles(db, broken)
    assert (
        db.execute("SELECT count(*) FROM chaika.employee_roles WHERE present_in_latest").fetchone()[
            0
        ]
        == 1
    )


def test_uuid_links_keep_main_list_order_duplicates_and_unresolved_refs(db, sample):
    snapshot = stage(db, sample)
    sync.publish_employee_roles(db, snapshot)
    db.execute(
        "INSERT INTO chaika.employees(source_id,id,code,name,main_role_id,role_ids,role_codes,"
        "first_seen_at,last_seen_at,last_snapshot_id,details) "
        "VALUES('primary',%s,'employee','Test employee',%s,%s,%s,now(),now(),%s,'{}')",
        (
            uuid4(),
            UUID(int=1),
            [UUID(int=2), UUID(int=99), None, UUID(int=2)],
            ["unrelated-code"],
            snapshot["id"],
        ),
    )
    other = Source("other", "Other", "https://other.example/resto/api", "b" * 64)
    register_sources(db, [other])
    db.execute(
        "INSERT INTO chaika.employee_roles SELECT 'other',%s,code,'Wrong source',"
        "payment_per_hour,steady_salary,schedule_type,deleted,present_in_latest,"
        "first_seen_at,last_seen_at,last_snapshot_id,details FROM chaika.employee_roles LIMIT 1",
        (UUID(int=99),),
    )
    counts = sync.publish_employee_roles(db, snapshot)
    assert counts["referenced_role_ids"] == 3 and counts["unresolved_role_ids"] == 1
    assert counts["employees_main_role_resolved"] == 1
    rows = db.execute(
        "SELECT assignment_kind,ordinal,role_name FROM chaika.employee_role_assignments "
        "WHERE source_id='primary' ORDER BY assignment_kind,ordinal"
    ).fetchall()
    assert rows == [
        ("list", 1, "Role 2"),
        ("list", 2, None),
        ("list", 4, "Role 2"),
        ("main", 0, "Role 1"),
    ]
    for table in ("employee_roles", "employee_role_assignments"):
        for user in ("anon", "authenticated"):
            assert not db.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT')", (user, f"chaika.{table}")
            ).fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("DELETE FROM chaika.employee_roles")
    assert (
        "security_invoker=true"
        in db.execute(
            "SELECT reloptions FROM pg_class WHERE oid='chaika.employee_role_assignments'::regclass"
        ).fetchone()[0]
    )


@pytest.fixture
def runtime(db, sample, monkeypatch):
    _, source, folder, response = sample
    settings = config(database_url="test-only", sync_api_key="test-sync-key")
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    monkeypatch.setattr(sync, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync, "BACKEND_DIR", folder.parent.parent)
    monkeypatch.setattr(sync.psycopg, "connect", lambda *a, **k: nullcontext(db))
    calls, responses = [], {"load": 200, "logout": 200}

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/load"):
            assert request.headers["X-Sync-Key"] == "test-sync-key"
            return httpx.Response(responses["load"], json=response)
        return httpx.Response(responses["logout"], json={"state": "logged_out"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8010")
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    return settings, db, calls, responses


def test_worker_commits_catalogue_and_logs_out(runtime):
    settings, db, calls, _ = runtime
    report = sync.synchronize_employee_roles(settings)
    assert report["counts"]["employee_roles"] == 2 and report["counts"]["logout_ok"] is True
    assert calls[-1].endswith("/logout")
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "succeeded"
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["load", "logout", "publish"])
def test_worker_failure_rolls_back_raw_and_attempts_logout(runtime, monkeypatch, failure):
    settings, db, calls, responses = runtime
    if failure == "publish":

        def broken(*_):
            raise ValueError("secret-internal-message")

        monkeypatch.setattr(sync, "publish_employee_roles", broken)
    else:
        responses[failure] = 502
    with pytest.raises(SyncError) as error:
        sync.synchronize_employee_roles(settings)
    assert "secret-internal" not in str(error.value)
    assert calls[-1].endswith("/logout")
    assert db.execute("SELECT count(*) FROM chaika.employee_roles").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "failed"


def test_http_contract_pagination_restart_and_auth(tmp_path):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, text=str(UUID(int=42)))
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, text=str(UUID(int=42)))
        assert request.url.path.endswith("/employees/roles")
        assert request.url.params["revisionFrom"] == "-1"
        assert "includeDeleted" not in request.url.params
        return httpx.Response(
            200, content=xml(role() + role(2)), headers={"Content-Type": "application/xml"}
        )

    settings = config(sync_api_key="test-sync-key")
    kwargs = dict(iiko_transport=httpx.MockTransport(handle), employee_roles_directory=tmp_path)
    with TestClient(create_app(settings, **kwargs)) as client:
        assert client.post("/api/v1/iiko/employee-roles/load").status_code == 401
        assert client.get("/api/v1/iiko/employee-roles").status_code == 401
        assert not calls
        loaded = client.post("/api/v1/iiko/employee-roles/load", headers=HEADERS)
        assert loaded.status_code == 200 and loaded.json()["total"] == 2
        assert len(calls) == 2
        page = client.get("/api/v1/iiko/employee-roles?offset=1&limit=1", headers=HEADERS)
        assert page.status_code == 200 and page.json()["items"][0]["name"] == "Role 2"
        assert page.headers["cache-control"] == "no-store" and len(calls) == 2
        assert (
            client.get("/api/v1/iiko/employee-roles?limit=201", headers=HEADERS).status_code == 422
        )
        assert client.post("/api/v1/iiko/logout").status_code == 200
    before = len(calls)
    with TestClient(create_app(settings, **kwargs)) as client:
        assert (
            client.get("/api/v1/iiko/employee-roles", headers=HEADERS).json()["snapshot"]["total"]
            == 2
        )
    assert len(calls) == before


def test_sync_route_hides_errors_and_returns_lock_conflict(monkeypatch):
    code = ["private-path"]

    def fail(_):
        raise SyncError(code[0])

    monkeypatch.setattr(routes, "synchronize_employee_roles", fail)
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as client:
        url = "/api/v1/sync/employee-roles"
        assert client.post(url).status_code == 401
        response = client.post(url, headers=HEADERS)
        assert response.status_code == 503 and code[0] not in response.text
        code[0] = "sync_already_running"
        assert client.post(url, headers=HEADERS).status_code == 409
