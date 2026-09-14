"""Account provenance, hierarchy, atomicity and source-scoped balance joins."""

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
from test_iiko_dictionaries import PAYLOADS
from test_iiko_employees import config
from test_reference_sync import db as db

from app import sync_accounts as sync
from app import sync_dictionaries
from app.api.routes import sync_jobs as routes
from app.integrations.iiko.errors import IikoError
from app.main import create_app
from app.schemas.iiko_dictionaries import DictionarySnapshot
from app.services.iiko_dictionaries import read_dictionary, summarize_dictionary
from app.sync_references import Source, SyncError, append_snapshot, register_sources


def account(i=1, **fields):
    return {**json.loads(PAYLOADS["accounts"])[0], "id": str(UUID(int=i)), **fields}


@pytest.fixture
def sample(tmp_path, monkeypatch):
    source = Source("primary", "Chain", "https://iiko.example.test/resto/api", "a" * 64)
    raw = json.dumps(
        [account(), account(2, accountParentId=str(UUID(int=1)), deleted=True)]
    ).encode()
    folder = tmp_path / ".local/dictionaries/accounts"
    folder.mkdir(parents=True)
    key = uuid4()
    path = folder / f"{key}.json"
    path.write_bytes(raw)
    rows = read_dictionary(path, len(raw), kind="accounts")
    response = DictionarySnapshot(
        snapshot_id=key,
        received_at=datetime.now(UTC),
        total=2,
        source_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        **summarize_dictionary(rows, kind="accounts"),
    ).model_dump(mode="json")
    (folder / "current.json").write_text(
        json.dumps({"source_fingerprint": source.fingerprint, "snapshot": response})
    )
    monkeypatch.setattr(sync_dictionaries, "BACKEND_DIR", tmp_path)
    snapshot = sync.capture_dictionary(config(), source, "accounts", response)
    return source, snapshot, response


def stage(db, sample):
    source, snapshot, _ = sample
    register_sources(db, [source])
    run = uuid4()
    db.execute(
        "INSERT INTO chaika.sync_runs(id,job,status) VALUES(%s,'accounts','running')", (run,)
    )
    append_snapshot(db, run, snapshot)
    return run, snapshot


def test_hierarchy_nullable_codes_future_type_and_strict_fields(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([account(type="FUTURE_TYPE", future={"a": 1})]))
    row = read_dictionary(path, 10000, kind="accounts")[0]
    assert row.type == "FUTURE_TYPE" and row.code is None and row.account_parent_id is None
    for change in [
        {"deleted": "false"},
        {"rootType": "MeasureUnit"},
        {"accountParentId": "bad"},
        {"customTransactionsAllowed": 1},
    ]:
        path.write_text(json.dumps([account(**change)]))
        with pytest.raises(IikoError):
            read_dictionary(path, 10000, kind="accounts")
    path.write_text(json.dumps([account()]).replace('"code": null', '"code": null, "code": "1"'))
    with pytest.raises(IikoError):
        read_dictionary(path, 10000, kind="accounts")


def test_repeated_publication_absence_return_and_stale_snapshot(db, sample):
    run, snapshot = stage(db, sample)
    first = sync.publish_accounts(db, snapshot)
    assert first["accounts"] == 2 and first["deleted"] == 1
    assert first["parent_accounts_ids"] == 1 and first["parent_accounts_unresolved"] == 0
    sync.publish_accounts(db, snapshot)
    assert db.execute("SELECT count(*) FROM chaika.accounts").fetchone()[0] == 2
    next_snap = deepcopy(snapshot)
    next_snap["id"] = str(uuid4())
    next_snap["observed_at"] += timedelta(seconds=1)
    next_snap["payload"].update(
        snapshot_id=next_snap["id"],
        received_at=next_snap["observed_at"].isoformat(),
        total=1,
        without_code=1,
        deleted_count=0,
    )
    next_snap["payload"]["items"] = next_snap["payload"]["items"][:1]
    append_snapshot(db, run, next_snap)
    assert sync.publish_accounts(db, next_snap)["absent_from_latest"] == 1
    with pytest.raises(SyncError, match="stale"):
        sync.publish_accounts(db, snapshot)
    returned = deepcopy(snapshot)
    returned["id"] = str(uuid4())
    returned["observed_at"] += timedelta(seconds=2)
    returned["payload"].update(
        snapshot_id=returned["id"], received_at=returned["observed_at"].isoformat()
    )
    append_snapshot(db, run, returned)
    assert sync.publish_accounts(db, returned)["absent_from_latest"] == 0
    assert db.execute(
        "SELECT min(first_seen_at),max(last_seen_at) FROM chaika.accounts"
    ).fetchone() == (snapshot["observed_at"], returned["observed_at"])
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 3


@pytest.mark.parametrize("bad", ["empty", "duplicate", "scope", "time", "summary"])
def test_incomplete_observation_keeps_last_good_dictionary(db, sample, bad):
    _, snapshot = stage(db, sample)
    sync.publish_accounts(db, snapshot)
    broken = deepcopy(snapshot)
    if bad == "empty":
        broken["payload"].update(items=[], total=0, deleted_count=0, without_code=0)
    elif bad == "duplicate":
        broken["payload"]["items"][1]["id"] = broken["payload"]["items"][0]["id"]
    elif bad == "scope":
        broken["payload"]["request"]["includeDeleted"] = "false"
    elif bad == "time":
        broken["observed_at"] += timedelta(seconds=1)
    else:
        broken["payload"]["deleted_count"] = 9
    with pytest.raises(SyncError):
        sync.publish_accounts(db, broken)
    assert (
        db.execute("SELECT count(*) FROM chaika.accounts WHERE present_in_latest").fetchone()[0]
        == 2
    )


def test_balance_view_keeps_exact_rows_and_never_joins_other_sources(db, sample):
    _, snapshot = stage(db, sample)
    sync.publish_accounts(db, snapshot)
    register_sources(db, [Source("other", "Other", "https://other.example/resto/api", "b" * 64)])
    db.execute(
        "INSERT INTO chaika.accounts SELECT 'other',%s,root_type,code,'Other account',"
        "deleted,account_parent_id,parent_corporate_id,type,system,custom_transactions_allowed,"
        "present_in_latest,first_seen_at,last_seen_at,last_snapshot_id,details "
        "FROM chaika.accounts LIMIT 1",
        (UUID(int=99),),
    )
    key = UUID(snapshot["id"])
    for n, account_id, amount in [(1, 1, "-123.123456789123456789"), (2, 1, "0"), (3, 99, "42")]:
        db.execute(
            "INSERT INTO chaika.counteragent_balance_items"
            "(snapshot_id,line_num,account_id,counteragent_id,department_id,sum) "
            "VALUES(%s,%s,%s,NULL,NULL,%s)",
            (key, n, UUID(int=account_id), Decimal(amount)),
        )
    db.execute(
        "INSERT INTO chaika.counteragent_balance_reports VALUES"
        "('primary','2026-09-10T23:59:59',%s,3,now(),now())",
        (key,),
    )
    rows = db.execute(
        "SELECT line_num,account_resolved,account_name,sum,counteragent_id "
        "FROM chaika.counteragent_balances_with_accounts ORDER BY line_num"
    ).fetchall()
    assert rows == [
        (1, True, "Test account", Decimal("-123.123456789123456789"), None),
        (2, True, "Test account", Decimal(0), None),
        (3, False, None, Decimal(42), None),
    ]
    counts = sync.account_links(db, "primary")
    assert counts["balance_accounts_ids"] == 2 and counts["balance_accounts_unresolved"] == 1
    assert db.execute("SELECT count(*) FROM chaika.counteragent_balance_items").fetchone()[0] == 3
    for table in ("accounts", "counteragent_balances_with_accounts"):
        for role in ("anon", "authenticated"):
            assert not db.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT')", (role, f"chaika.{table}")
            ).fetchone()[0]
    assert (
        "security_invoker=true"
        in db.execute(
            "SELECT reloptions FROM pg_class "
            "WHERE oid='chaika.counteragent_balances_with_accounts'::regclass"
        ).fetchone()[0]
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("DELETE FROM chaika.accounts")


@pytest.fixture
def runtime(db, sample, monkeypatch):
    source, _, response = sample
    settings = config(database_url="test-only", sync_api_key="test-sync-key")
    register_sources(db, [source])
    db.execute("UPDATE chaika.sources SET server_type='CHAIN'")
    monkeypatch.setattr(sync, "configured_sources", lambda _: [source])
    monkeypatch.setattr(sync.psycopg, "connect", lambda *a, **k: nullcontext(db))
    calls, responses = [], {"load": 200, "logout": 200}

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(
                200, json={"items": [{"connection_id": "primary", "base_url": source.base_url}]}
            )
        if request.url.path.endswith("/load"):
            assert request.url.path == "/api/v1/iiko/dictionaries/accounts/load"
            return httpx.Response(responses["load"], json=response)
        return httpx.Response(responses["logout"], json={"state": "logged_out"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8010")
    monkeypatch.setattr(sync.httpx, "Client", lambda **kwargs: client)
    return settings, db, calls, responses


def test_worker_commits_and_releases_session(runtime):
    settings, db, calls, _ = runtime
    report = sync.synchronize_accounts(settings)
    assert report["counts"]["accounts"] == 2 and report["counts"]["logout_ok"] is True
    assert calls == [
        "/api/v1/iiko/connections",
        "/api/v1/iiko/dictionaries/accounts/load",
        "/api/v1/iiko/connections/primary/logout",
    ]
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "succeeded"
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["load", "logout", "publish"])
def test_worker_failure_rolls_back_and_releases_session(runtime, monkeypatch, failure):
    settings, db, calls, responses = runtime
    if failure == "publish":

        def broken(db, snapshot):
            sync.publish_accounts_original(db, snapshot)
            raise ValueError("private-message")

        monkeypatch.setattr(sync, "publish_accounts_original", sync.publish_accounts, raising=False)
        monkeypatch.setattr(sync, "publish_accounts", broken)
    else:
        responses[failure] = 502
    with pytest.raises(SyncError) as error:
        sync.synchronize_accounts(settings)
    assert "private-message" not in str(error.value)
    assert calls[-1].endswith("/logout")
    assert db.execute("SELECT count(*) FROM chaika.accounts").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chaika.raw_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT status FROM chaika.sync_runs").fetchone()[0] == "failed"


def test_sync_auth_conflict_and_error_redaction(tmp_path, monkeypatch):
    def blocked(_):
        raise SyncError("sync_already_running")

    monkeypatch.setattr(routes, "synchronize_accounts", blocked)
    with TestClient(
        create_app(config(sync_api_key="test-sync-key"), dictionaries_directory=tmp_path)
    ) as client:
        assert client.post("/api/v1/sync/accounts").status_code == 401
        response = client.post("/api/v1/sync/accounts", headers={"X-Sync-Key": "test-sync-key"})
        assert response.status_code == 409

        def broken(_):
            raise ValueError("private-message")

        monkeypatch.setattr(routes, "synchronize_accounts", broken)
        response = client.post("/api/v1/sync/accounts", headers={"X-Sync-Key": "test-sync-key"})
        assert response.status_code == 503 and "private-message" not in response.text
