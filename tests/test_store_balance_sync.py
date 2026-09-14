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
from test_iiko_store_balances import body, config, row
from test_reference_sync import db as db

from app import sync_store_balances as sync
from app.api.routes import sync_jobs as routes
from app.main import create_app
from app.schemas.iiko_reports import AccountingReportQuery
from app.schemas.iiko_store_balances import StoreBalancesQuery, StoreBalancesResponse
from app.services.iiko_store_balances import read_store_balances
from app.sync_references import Source, SyncError, append_snapshot, register_sources

ENDPOINT = "/api/v1/sync/store-balances"
HEADERS = {"X-Sync-Key": "test-sync-key"}
WHEN = "2026-09-09T23:59:59"


@pytest.fixture
def sample(tmp_path):
    source = Source("primary", "Chain", "https://iiko.example/resto/api", "a" * 64)
    query = AccountingReportQuery(timestamp=WHEN)
    folder = tmp_path / ".local/store-balances"
    folder.mkdir(parents=True)
    key = uuid4()
    raw = body([row(), row(amount=0, sum=0)])
    path = folder / f"{key}.json"
    path.write_bytes(raw)
    rows = read_store_balances(path, StoreBalancesQuery(timestamp=query.timestamp))
    response = StoreBalancesResponse(
        snapshot_id=key,
        received_at=datetime.now(UTC),
        request=StoreBalancesQuery(timestamp=query.timestamp),
        total=len(rows),
        items=rows,
        source_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    ).model_dump(mode="json")
    metadata = {k: v for k, v in response.items() if k != "items"}
    metadata.update(
        source_fingerprint=source.fingerprint, source_endpoint="v2/reports/balance/stores"
    )
    (folder / f"{key}.meta.json").write_text(json.dumps(metadata))
    return source, query, folder, response


def capture(sample):
    source, query, folder, response = sample
    return sync.capture_store_balances(config(), source, query, response, folder)


def test_capture_exact_numbers_and_duplicate_pairs(sample):
    snapshot = capture(sample)
    assert snapshot["payload"]["items"][0]["amount"] == "-0.123456789123456789"
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
        response["request"]["store_id"] = [str(uuid4())]
        metadata["request"] = response["request"]
    elif bad == "rows":
        response["items"][0]["amount"] = "99"
    else:
        response["total"] = metadata["total"] = 99
    path.write_text(json.dumps(metadata))
    with pytest.raises(SyncError):
        capture(sample)


def stage(db, sample):
    register_sources(db, [sample[0]])
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'store_balances','running')", (run,)
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
        "SELECT i.store_id,i.product_id,i.amount,i.sum FROM chaika.store_balance_reports r "
        "JOIN chaika.store_balance_items i ON i.snapshot_id=r.last_snapshot_id "
        "ORDER BY r.accounting_timestamp,i.line_num"
    ).fetchall()


def test_numeric_precision_duplicates_unknown_links_and_append_only(db, sample):
    _, snapshot = stage(db, sample)
    counts = sync.publish_store_balances(db, snapshot)
    assert counts["duplicate_pair_rows"] == 1
    assert counts["unmatched_stores"] == counts["unmatched_products"] == 1
    rows = current_rows(db)
    assert len(rows) == 2 and rows[0][2:] == (
        Decimal("-0.123456789123456789"),
        Decimal("-123.123456789123456789"),
    )
    assert rows[1][2:] == (Decimal(0), Decimal(0))
    for statement in [
        "UPDATE chaika.store_balance_items SET amount=0",
        "DELETE FROM chaika.store_balance_items",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(statement)
    for role in ["anon", "authenticated"]:
        assert not db.execute(
            "SELECT has_table_privilege(%s,'chaika.store_balance_items','SELECT')", (role,)
        ).fetchone()[0]


def test_empty_replacement_keeps_history_and_other_timestamp(db, sample):
    run, first = stage(db, sample)
    sync.publish_store_balances(db, first)
    other = next_observation(db, run, first, timestamp="2026-09-08T23:59:59")
    sync.publish_store_balances(db, other)
    empty = next_observation(db, run, first, size=0, seconds=2)
    sync.publish_store_balances(db, empty)
    assert len(current_rows(db)) == 2  # Only the other timestamp retains non-empty rows.
    report = db.execute(
        "SELECT row_count,first_seen_at,last_snapshot_id FROM chaika.store_balance_reports "
        "WHERE accounting_timestamp=%s",
        (sample[1].timestamp,),
    ).fetchone()
    assert report[0] == 0 and report[1] == first["observed_at"] and str(report[2]) == empty["id"]
    assert db.execute("SELECT count(*) FROM chaika.store_balance_items").fetchone()[0] == 4
    returned = next_observation(db, run, first, seconds=3)
    sync.publish_store_balances(db, returned)
    assert len(current_rows(db)) == 4
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 4


def test_older_observation_cannot_replace_newer_current_report(db, sample):
    run, first = stage(db, sample)
    sync.publish_store_balances(db, first)
    older = next_observation(db, run, first, size=0, seconds=-1)
    sync.publish_store_balances(db, older)
    assert len(current_rows(db)) == 2


def test_invalid_publication_rolls_back_inserted_items(db, sample):
    run, first = stage(db, sample)
    sync.publish_store_balances(db, first)
    broken = next_observation(db, run, first)
    broken["source_id"] = "missing-source"
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sync.publish_store_balances(db, broken)
    assert db.execute("SELECT count(*) FROM chaika.store_balance_items").fetchone()[0] == 2
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
        if request.url.path.endswith("/store-balances"):
            assert dict(request.url.params) == {"timestamp": WHEN}
            return httpx.Response(statuses["load"], json=response)
        assert request.url.path.endswith("/logout")
        return httpx.Response(statuses["logout"], json={"state": "logged_out"})

    client = httpx.Client(base_url="http://127.0.0.1:8010", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    return query, calls, statuses


def test_worker_success_is_atomic_and_logs_out(db, runtime):
    query, calls, _ = runtime
    report = sync.synchronize_store_balances(config(database_url="test-only"), query)
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

        monkeypatch.setattr(sync, "publish_store_balances", fail)
    else:
        statuses[failure] = 502
    with pytest.raises(SyncError) as error:
        sync.synchronize_store_balances(config(database_url="test-only"), query)
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

    monkeypatch.setattr(routes, "synchronize_store_balances", fail)
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as client:
        assert client.post(ENDPOINT, json={"timestamp": WHEN}).status_code == 401
        for payload in [
            {},
            {"timestamp": WHEN + "Z"},
            {"timestamp": WHEN, "store_id": [str(uuid4())]},
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

    monkeypatch.setattr(routes, "synchronize_store_balances", busy)
    with TestClient(create_app(config(sync_api_key="test-sync-key"))) as client:
        assert client.post(ENDPOINT, json={"timestamp": WHEN}, headers=HEADERS).status_code == 409
