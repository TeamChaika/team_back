import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from test_iiko_balances import body, config, row
from test_reference_sync import db as db

from app import sync_counteragent_balances as sync
from app.api.routes import sync_jobs as routes
from app.main import create_app
from app.schemas.iiko_balances import CounteragentBalancesQuery, CounteragentBalancesResponse
from app.schemas.iiko_reports import AccountingReportQuery
from app.services.iiko_balances import read_counteragent_balances
from app.sync_references import Source, SyncError, append_snapshot, register_sources

ENDPOINT = "/api/v1/sync/counteragent-balances"
HEADERS = {"X-Sync-Key": "test-sync-key"}
WHEN = "2026-09-09T23:59:59"


@pytest.fixture
def sample(tmp_path):
    source = Source("primary", "Chain", "https://iiko.example/resto/api", "a" * 64)
    query = AccountingReportQuery(timestamp=WHEN)
    folder = tmp_path / ".local/counteragent-balances"
    folder.mkdir(parents=True)
    key = uuid4()
    raw = body([row(), row(sum=0)])
    path = folder / f"{key}.json"
    path.write_bytes(raw)
    rows = read_counteragent_balances(path, CounteragentBalancesQuery(timestamp=query.timestamp))
    response = CounteragentBalancesResponse(
        snapshot_id=key,
        received_at=datetime.now(UTC),
        request=CounteragentBalancesQuery(timestamp=query.timestamp),
        total=len(rows),
        items=rows,
        source_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    ).model_dump(mode="json")
    metadata = {k: v for k, v in response.items() if k != "items"}
    metadata.update(
        source_fingerprint=source.fingerprint, source_endpoint="v2/reports/balance/counteragents"
    )
    (folder / f"{key}.meta.json").write_text(json.dumps(metadata))
    return source, query, folder, response


def capture(sample):
    source, query, folder, response = sample
    return sync.capture_counteragent_balances(config(), source, query, response, folder)


def test_capture_exact_numbers_and_duplicate_dimensions(sample):
    snapshot = capture(sample)
    assert snapshot["payload"]["items"][0]["sum"] == "-123.123456789123456789"
    assert snapshot["payload"]["total"] == 2


@pytest.mark.parametrize("bad", ["source", "endpoint", "hash", "scope", "rows", "count"])
def test_capture_rejects_wrong_source_scope_or_content(sample, bad):
    _, _, folder, response = sample
    path = folder / f"{response['snapshot_id']}.meta.json"
    metadata = json.loads(path.read_text())
    if bad == "source":
        metadata["source_fingerprint"] = "b" * 64
    elif bad == "endpoint":
        metadata["source_endpoint"] = "wrong"
    elif bad == "hash":
        (folder / f"{response['snapshot_id']}.json").write_bytes(b"[]")
    elif bad == "scope":
        response["request"]["account_id"] = [str(uuid4())]
        metadata["request"] = response["request"]
    elif bad == "rows":
        response["items"][0]["sum"] = "99"
    else:
        response["total"] = metadata["total"] = 99
    path.write_text(json.dumps(metadata))
    with pytest.raises(SyncError):
        capture(sample)


def stage(db, sample):
    register_sources(db, [sample[0]])
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'counteragent_balances','running')",
        (run,),
    )
    snapshot = capture(sample)
    append_snapshot(db, run, snapshot)
    return run, snapshot


def next_observation(db, run, original, *, size=2, seconds=1, timestamp=None):
    snapshot = deepcopy(original)
    snapshot["id"] = str(uuid4())
    snapshot["observed_at"] += timedelta(seconds=seconds)
    payload = snapshot["payload"]
    payload.update(
        snapshot_id=snapshot["id"], received_at=snapshot["observed_at"].isoformat(), total=size
    )
    payload["items"] = payload["items"][:size]
    if timestamp:
        payload["request"]["timestamp"] = timestamp
    append_snapshot(db, run, snapshot)
    return snapshot


def current_rows(db):
    return db.execute(
        "SELECT i.account_id,i.counteragent_id,i.department_id,i.sum "
        "FROM chaika.counteragent_balance_reports r "
        "JOIN chaika.counteragent_balance_items i ON i.snapshot_id=r.last_snapshot_id "
        "ORDER BY r.accounting_timestamp,i.line_num"
    ).fetchall()


def test_numeric_precision_duplicates_unknown_links_and_append_only(db, sample):
    _, snapshot = stage(db, sample)
    counts = sync.publish_counteragent_balances(db, snapshot)
    assert counts["duplicate_dimension_rows"] == 1
    assert counts["unmatched_counteragents"] == counts["unmatched_departments"] == 1
    rows = current_rows(db)
    assert len(rows) == 2 and rows[0][3] == Decimal("-123.123456789123456789")
    assert rows[1][3] == Decimal(0)
    for statement in [
        "UPDATE chaika.counteragent_balance_items SET sum=0",
        "DELETE FROM chaika.counteragent_balance_items",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(statement)
    for role in ["anon", "authenticated"]:
        assert not db.execute(
            "SELECT has_table_privilege(%s,'chaika.counteragent_balance_items','SELECT')", (role,)
        ).fetchone()[0]


def test_empty_replacement_keeps_history_and_other_timestamp(db, sample):
    run, first = stage(db, sample)
    sync.publish_counteragent_balances(db, first)
    other = next_observation(db, run, first, timestamp="2026-09-08T23:59:59")
    sync.publish_counteragent_balances(db, other)
    empty = next_observation(db, run, first, size=0, seconds=2)
    sync.publish_counteragent_balances(db, empty)
    assert len(current_rows(db)) == 2  # Only the other timestamp retains non-empty rows.
    report = db.execute(
        "SELECT row_count,first_seen_at,last_snapshot_id FROM chaika.counteragent_balance_reports "
        "WHERE accounting_timestamp=%s",
        (sample[1].timestamp,),
    ).fetchone()
    assert report[0] == 0 and report[1] == first["observed_at"] and str(report[2]) == empty["id"]
    assert db.execute("SELECT count(*) FROM chaika.counteragent_balance_items").fetchone()[0] == 4
    returned = next_observation(db, run, first, seconds=3)
    sync.publish_counteragent_balances(db, returned)
    assert len(current_rows(db)) == 4
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 4


def test_older_observation_cannot_replace_newer_current_report(db, sample):
    run, first = stage(db, sample)
    sync.publish_counteragent_balances(db, first)
    older = next_observation(db, run, first, size=0, seconds=-1)
    sync.publish_counteragent_balances(db, older)
    assert len(current_rows(db)) == 2


def test_null_dimensions_are_preserved_and_not_counted_as_unmatched(db, sample):
    _, snapshot = stage(db, sample)
    for item in snapshot["payload"]["items"]:
        item["counteragent_id"] = item["department_id"] = None
    counts = sync.publish_counteragent_balances(db, snapshot)
    assert counts["null_counteragent_rows"] == counts["null_department_rows"] == 2
    assert counts["counteragents"] == counts["departments"] == 0
    assert counts["unmatched_counteragents"] == counts["unmatched_departments"] == 0
    assert all(r[1:3] == (None, None) for r in current_rows(db))


@pytest.mark.parametrize("table", ["employees", "counteragents"])
def test_counterparty_can_be_supplier_or_employee(db, sample, table):
    _, snapshot = stage(db, sample)
    item = snapshot["payload"]["items"][0]
    # Counterparty UUIDs in this report are not restricted to the supplier directory.
    db.execute(
        sql.SQL(
            "INSERT INTO chaika.{}(source_id,id,code,name,first_seen_at,last_seen_at,"
            "last_snapshot_id,details) VALUES(%s,%s,'','Reference',%s,%s,%s,'{{}}')"
        ).format(sql.Identifier(table)),
        (
            snapshot["source_id"],
            item["counteragent_id"],
            snapshot["observed_at"],
            snapshot["observed_at"],
            snapshot["id"],
        ),
    )
    db.execute(
        "INSERT INTO chaika.corporate_nodes(source_id,id,name,type,first_seen_at,last_seen_at,"
        "last_snapshot_id) VALUES(%s,%s,'Department','DEPARTMENT',%s,%s,%s)",
        (
            snapshot["source_id"],
            item["department_id"],
            snapshot["observed_at"],
            snapshot["observed_at"],
            snapshot["id"],
        ),
    )
    counts = sync.publish_counteragent_balances(db, snapshot)
    assert counts["unmatched_counteragents"] == counts["unmatched_departments"] == 0
    assert counts["accounts_without_dictionary"] == 1


@pytest.mark.parametrize("bad", ["resource", "account_id", "counteragent_id", "department_id"])
def test_partial_scope_or_wrong_resource_cannot_replace_full_report(db, sample, bad):
    run, first = stage(db, sample)
    sync.publish_counteragent_balances(db, first)
    partial = next_observation(db, run, first)
    if bad == "resource":
        partial["resource"] = "store_balances"
    else:
        partial["payload"]["request"][bad] = [str(uuid4())]
    with pytest.raises(SyncError):
        sync.publish_counteragent_balances(db, partial)
    assert db.execute("SELECT count(*) FROM chaika.counteragent_balance_items").fetchone()[0] == 2


def test_invalid_publication_rolls_back_inserted_items(db, sample):
    run, first = stage(db, sample)
    sync.publish_counteragent_balances(db, first)
    broken = next_observation(db, run, first)
    broken["source_id"] = "missing-source"
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sync.publish_counteragent_balances(db, broken)
    assert db.execute("SELECT count(*) FROM chaika.counteragent_balance_items").fetchone()[0] == 2
    assert len(current_rows(db)) == 2


@pytest.fixture
def runtime(db, sample, monkeypatch):
    source, query, folder, response = sample
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    monkeypatch.setattr(sync, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync, "BACKEND_DIR", folder.parent.parent)
    monkeypatch.setattr(sync.psycopg, "connect", lambda *a, **k: nullcontext(db))
    calls, statuses = [], {"load": 200, "logout": 200}

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/counteragent-balances"):
            assert dict(request.url.params) == {"timestamp": WHEN}
            return httpx.Response(statuses["load"], json=response)
        assert request.url.path.endswith("/logout")
        return httpx.Response(statuses["logout"], json={"state": "logged_out"})

    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    return query, calls, statuses


def test_worker_success_is_atomic_and_logs_out(db, runtime):
    query, calls, _ = runtime
    report = sync.synchronize_counteragent_balances(config(database_url="test-only"), query)
    assert report["counts"]["rows"] == 2 and report["counts"]["logout_ok"] is True
    assert calls[-1].url.path.endswith("/logout")
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "succeeded"
    assert len(current_rows(db)) == 2


@pytest.mark.parametrize("failure", ["load", "logout", "publish"])
def test_worker_failure_preserves_database_and_releases_session(db, runtime, monkeypatch, failure):
    query, calls, statuses = runtime
    if failure == "publish":

        def fail(*args):
            raise ValueError("private-snapshot")

        monkeypatch.setattr(sync, "publish_counteragent_balances", fail)
    else:
        statuses[failure] = 502
    with pytest.raises(SyncError) as error:
        sync.synchronize_counteragent_balances(config(database_url="test-only"), query)
    assert "private-snapshot" not in str(error.value)
    assert calls[-1].url.path.endswith("/logout")
    assert not current_rows(db)
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "failed"


def test_api_auth_validation_and_failure_redaction(monkeypatch):
    calls = []

    def fail(*args):
        calls.append(1)
        raise SyncError("private-response")

    monkeypatch.setattr(routes, "synchronize_counteragent_balances", fail)
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as client:
        assert client.post(ENDPOINT, json={"timestamp": WHEN}).status_code == 401
        for payload in [
            {},
            {"timestamp": WHEN + "Z"},
            {"timestamp": WHEN, "account_id": [str(uuid4())]},
        ]:
            assert client.post(ENDPOINT, json=payload, headers=HEADERS).status_code == 422
        assert not calls
        response = client.post(ENDPOINT, json={"timestamp": WHEN}, headers=HEADERS)
        assert response.status_code == 503 and "private-response" not in response.text
        assert client.get("/openapi.json").json()["paths"][ENDPOINT]["post"]["security"] == [
            {"SyncKey": []}
        ]


def test_api_busy_is_409(monkeypatch):
    def busy(*args):
        raise SyncError("sync_already_running")

    monkeypatch.setattr(routes, "synchronize_counteragent_balances", busy)
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as client:
        assert client.post(ENDPOINT, json={"timestamp": WHEN}, headers=HEADERS).status_code == 409


@pytest.mark.parametrize("same_source", [True, False])
def test_account_resolution_uses_source_uuid_without_changing_money(db, sample, same_source):
    _, snapshot = stage(db, sample)
    account_id = snapshot["payload"]["items"][0]["account_id"]
    source_id = "primary" if same_source else "another-chain"
    if not same_source:
        register_sources(
            db, [Source(source_id, "Other", "https://another.example/resto/api", "b" * 64)]
        )
    db.execute(
        "INSERT INTO chaika.accounts(source_id,id,root_type,code,name,deleted,account_parent_id,"
        "parent_corporate_id,type,system,custom_transactions_allowed,present_in_latest,"
        "first_seen_at,last_seen_at,last_snapshot_id,details) "
        "VALUES(%s,%s,'Account',NULL,'Historical account',true,NULL,NULL,'CASH',false,true,"
        "false,now(),now(),%s,'{}')",
        (source_id, account_id, snapshot["id"]),
    )
    counts = sync.publish_counteragent_balances(db, snapshot)
    assert counts["accounts_without_dictionary"] == (0 if same_source else 1)
    assert [r[-1] for r in current_rows(db)] == [Decimal("-123.123456789123456789"), Decimal(0)]
